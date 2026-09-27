"""Demo-only REST/WS. No generic URL fallback, redirects, transfers or key logging."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
import httpx
import websockets
from .contracts import SafetyError

REST = "https://api-demo.bybit.com"
PRIVATE_WS = "wss://stream-demo.bybit.com/v5/private"
READS = frozenset({"/v5/user/query-api", "/v5/account/info", "/v5/account/wallet-balance",
    "/v5/account/transaction-log", "/v5/position/list", "/v5/order/realtime", "/v5/order/history",
    "/v5/execution/list"})
WRITES = frozenset({"/v5/order/create", "/v5/order/cancel", "/v5/order/amend"})

@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    expected_uid: str = field(repr=False)

    def __post_init__(self):
        if not all((self.key, self.secret, self.expected_uid)):
            raise SafetyError("local Demo credentials and expected UID required")

class DemoReject(SafetyError):
    def __init__(self, code):
        self.code = int(code)
        super().__init__("Demo API rejection code " + str(self.code))

class DemoRest:
    def __init__(self, credentials, *, enabled=False, base_url=REST, transport=None):
        if not enabled or base_url != REST: raise SafetyError("Demo adapter disabled or host forbidden")
        self.credentials = credentials
        self.client = httpx.AsyncClient(base_url=REST, timeout=5, follow_redirects=False,
                                        trust_env=False, transport=transport)
        self.write_enabled = False
        self.before_write = None
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, params=None):
        if (method == "GET" and path not in READS) or (method == "POST" and path not in WRITES) or method not in ("GET", "POST"):
            raise SafetyError("private endpoint forbidden")
        if method == "POST" and not self.write_enabled: raise SafetyError("no-order preflight")
        params = dict(params or {})
        query = urlencode(sorted(params.items())) if method == "GET" else json.dumps(params, separators=(",", ":"))
        # No automatic mutation retry. Caller must resolve uncertain writes by ID.
        async with self._lock:
            await asyncio.sleep(max(0, .12 - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            if method=="POST" and self.before_write is not None:self.before_write(path,params)
            stamp = str(int(time.time()*1000)); window = "5000"
            sign = hmac.new(self.credentials.secret.encode(),
                (stamp+self.credentials.key+window+query).encode(), hashlib.sha256).hexdigest()
            headers = {"X-BAPI-API-KEY":self.credentials.key,"X-BAPI-SIGN":sign,
                "X-BAPI-TIMESTAMP":stamp,"X-BAPI-RECV-WINDOW":window,"Content-Type":"application/json"}
            try:
                response = await self.client.request(method, path+("?"+query if method=="GET" and query else ""),
                    content=query if method=="POST" else None, headers=headers)
            except httpx.HTTPError:
                raise SafetyError("Demo transport outcome unknown") from None
        if response.is_redirect or str(response.url).split("/v5/")[0] != REST:
            raise SafetyError("Demo redirect/host forbidden")
        if response.status_code != 200: raise SafetyError("Demo HTTP status " + str(response.status_code))
        try: row = response.json()
        except ValueError: raise SafetyError("invalid Demo response") from None
        if row.get("retCode") != 0: raise DemoReject(row.get("retCode", -1))
        return row["result"]

    async def pages(self, path, params=None):
        result=[]; seen=set(); params=dict(params or {})
        for _ in range(100):
            page=await self.request("GET",path,params)
            result.extend(page.get("list", []))
            cursor=page.get("nextPageCursor")
            if not cursor: return result
            if cursor in seen: raise SafetyError("pagination cycle")
            seen.add(cursor);params["cursor"]=cursor
        raise SafetyError("pagination bound exceeded")


class NoRedirectConnect(websockets.connect):
    def process_redirect(self, exc):
        return SafetyError("WebSocket redirect forbidden")


async def private_stream(credentials, on_message, on_gap, stop, *, url=PRIVATE_WS):
    if url != PRIVATE_WS: raise SafetyError("private WS host forbidden")
    # No automatic redirect-following reconnect iterator. Each failed connection
    # remains a gap until a fresh auth/subscription and REST reconciliation.
    while not stop.is_set():
        try:
            async with NoRedirectConnect(url, proxy=None, open_timeout=5, close_timeout=1,
                    max_queue=128, ping_interval=15, ping_timeout=10) as ws:
                expires=int(time.time()*1000)+10000
                sign=hmac.new(credentials.secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
                await ws.send(json.dumps({"op":"auth","args":[credentials.key,expires,sign]}))
                auth=json.loads(await asyncio.wait_for(ws.recv(),5))
                if auth.get("op")!="auth" or auth.get("success") is not True: raise SafetyError("Demo WS auth failed")
                await ws.send(json.dumps({"op":"subscribe","args":["order","execution","position"]}))
                reply=json.loads(await asyncio.wait_for(ws.recv(),5))
                if reply.get("success") is not True: raise SafetyError("Demo WS subscription failed")
                await on_gap("reconnected")
                while not stop.is_set():
                    try: raw=await asyncio.wait_for(ws.recv(),1)
                    except asyncio.TimeoutError: continue
                    await on_message(json.loads(raw))
        except asyncio.CancelledError: raise
        except Exception:
            await on_gap("private_ws_gap")
            try: await asyncio.wait_for(stop.wait(),1)
            except asyncio.TimeoutError: pass
