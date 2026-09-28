"""Unauthenticated public market streams; no private paths or execution methods."""
import asyncio
from dataclasses import asdict
import json
import time

import httpx
import websockets

from .cross_venue import VenueEvent
from .native_dispatch import create_task as native_create_task
from .native_v5 import NativeTapeError

BINANCE_REST = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_WS = "wss://fstream.binance.com/stream"
OKX_REST = "https://www.okx.com/api/v5/public/instruments"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"


def instruments(venue, payload):
    """Only exact base/USDT linear perpetual matches; never infer multiplier coins."""
    result = {}
    for row in payload["symbols" if venue == "binance" else "data"]:
        if venue == "binance":
            if (row.get("contractType") != "PERPETUAL" or row.get("status") != "TRADING"
                    or row.get("quoteAsset") != "USDT" or row.get("marginAsset") != "USDT"):
                continue
            symbol = row["baseAsset"]+"USDT"
            if row["symbol"] != symbol:
                continue
            result[symbol] = dict(instrument=row["symbol"], base=row["baseAsset"], multiplier=1., raw=row)
        else:
            if (row.get("instType") != "SWAP" or row.get("ctType") != "linear"
                    or row.get("settleCcy") != "USDT" or row.get("state") != "live"):
                continue
            base = row["instId"].removesuffix("-USDT-SWAP")
            if row["instId"] != base+"-USDT-SWAP" or row.get("ctValCcy") != base:
                continue
            multiplier = float(row["ctVal"])*float(row.get("ctMult") or 1)
            if multiplier <= 0:
                continue
            result[base+"USDT"] = dict(instrument=row["instId"], base=base, multiplier=multiplier, raw=row)
    return result


def decode(venue, raw, *, capture_id, symbol, epoch, spec, receipt_wall_ms, receipt_ns, processing_ns):
    payload = raw.get("data", raw) if venue == "binance" else raw
    common = dict(capture_id=capture_id, venue=venue, symbol=symbol, epoch=epoch,
        receipt_wall_ms=receipt_wall_ms, receipt_mono_ns=receipt_ns, processing_mono_ns=processing_ns,
        units_verified=True)
    if venue == "binance":
        if payload.get("s") != spec["instrument"]:
            return []
        if payload.get("e") == "depthUpdate":
            bids, asks = payload["b"], payload["a"]
            if not bids or not asks:
                raise ValueError("empty Binance depth snapshot")
            return [VenueEvent(**common, kind="quote", exchange_ms=int(payload["T"]), sequence=int(payload["u"]),
                bid=float(bids[0][0]), bid_qty=float(bids[0][1]), ask=float(asks[0][0]), ask_qty=float(asks[0][1]),
                previous_sequence=int(payload["pu"]) if "pu" in payload else None)]
        if payload.get("e") == "aggTrade":
            return [VenueEvent(**common, kind="trade", exchange_ms=int(payload["T"]), sequence=int(payload["a"]),
                price=float(payload["p"]), quantity=float(payload["q"]), side="sell" if payload["m"] else "buy")]
        return []
    if raw.get("arg", {}).get("instId") != spec["instrument"]:
        return []
    channel = raw["arg"]["channel"]
    events = []
    for row in raw.get("data", []):
        if channel == "books5":
            if not row["bids"] or not row["asks"]:
                raise ValueError("empty OKX snapshot")
            bid, ask = row["bids"][0], row["asks"][0]
            events.append(VenueEvent(**common, kind="quote", exchange_ms=int(row["ts"]),
                sequence=int(row.get("seqId", row["ts"])), bid=float(bid[0]), ask=float(ask[0]),
                bid_qty=float(bid[1])*spec["multiplier"], ask_qty=float(ask[1])*spec["multiplier"]))
        elif channel == "trades":
            events.append(VenueEvent(**common, kind="trade", exchange_ms=int(row["ts"]),
                sequence=int(row["tradeId"]), price=float(row["px"]),
                quantity=float(row["sz"])*spec["multiplier"], side=row["side"]))
    return events


class PublicCrossVenueService:
    def __init__(self, runtime, journal, *, clock=time, native_dispatch=None,
                 client_factory=None, connect_factory=None):
        self.runtime, self.journal, self.clock = runtime, journal, clock
        self.native_dispatch = native_dispatch
        if native_dispatch is not None:
            self.clock = native_dispatch.clock()
        self.specs = {}
        self.tasks = {}
        self.epochs = {}
        self.closed = False
        self.retired = set()
        self.client_factory = client_factory
        self.connect_factory = connect_factory

    def _record(self, kind, payload):
        if self.native_dispatch is not None:
            self.native_dispatch.boundary("external_"+kind, payload)
        self.journal.append(kind, payload)

    async def _receive(self, ws, timeout):
        task = native_create_task(self.native_dispatch, ws.recv(), name="cross-receive", module="cross_venue")
        return await asyncio.wait_for(task, timeout)

    async def start(self):
        async with (self.client_factory or httpx.AsyncClient)(timeout=10, trust_env=False, follow_redirects=False) as client:
            for venue, url in (("binance", BINANCE_REST), ("okx", OKX_REST)):
                try:
                    response = await client.get(url, params={"instType": "SWAP"} if venue == "okx" else None)
                    response.raise_for_status()
                    payload = response.json()
                    self.specs[venue] = instruments(venue, payload)
                    self._record("venue_instruments", dict(venue=venue,
                        receiptWallMs=int(self.clock.time()*1000), payload=payload, url=url))
                except NativeTapeError:
                    raise
                except Exception as exc:
                    self.specs[venue] = {}
                    self._record("venue_error", dict(venue=venue, reason=type(exc).__name__, stage="instruments"))

    def watch(self, symbols):
        wanted = {(v, s) for v, specs in self.specs.items() for s in symbols if s in specs}
        for key in sorted(set(self.tasks)-wanted):
            task = self.tasks.pop(key)
            self.retired.add(task)
            task.cancel()
        # Retain cancelled owners until their cleanup has finished and retrieve
        # failures; removal from the watch set is not a terminal/join receipt.
        for task in list(self.retired):
            if task.done():
                if not task.cancelled():
                    task.exception()
                self.retired.remove(task)
        for key in sorted(wanted-self.tasks.keys()):
            self.tasks[key] = native_create_task(self.native_dispatch, self._stream(*key),
                name="cross-stream:"+":".join(key), module="cross_venue")

    async def _stream(self, venue, symbol):
        key = venue, symbol
        spec = self.specs[venue][symbol]
        attempts = 0
        while not self.closed:
            self.epochs[key] = self.epochs.get(key, 0)+1
            epoch = self.epochs[key]
            self.runtime.gap(venue, symbol, epoch, "connecting")
            self._record("venue_gap", dict(venue=venue, symbol=symbol, epoch=epoch, reason="connecting"))
            try:
                streams = f"{symbol.lower()}@depth5@100ms/{symbol.lower()}@aggTrade"
                url = BINANCE_WS+"?streams="+streams if venue == "binance" else OKX_WS
                async with (self.connect_factory or websockets.connect)(url, open_timeout=10, close_timeout=2, max_queue=16,
                        max_size=1_048_576, proxy=None, ping_interval=20, ping_timeout=20) as ws:
                    if venue == "okx":
                        await ws.send(json.dumps(dict(op="subscribe", args=[dict(channel=c, instId=spec["instrument"])
                            for c in ("books5", "trades")])))
                    while not self.closed:
                        try:
                            raw = await self._receive(ws, 20)
                        except asyncio.TimeoutError:
                            if venue == "okx":
                                await ws.send("ping")
                                raw = await self._receive(ws, 5)
                            else:
                                raise
                        receipt_ns, wall = self.clock.perf_counter_ns(), int(self.clock.time()*1000)
                        if raw == "pong":
                            continue
                        payload = json.loads(raw)
                        processing_ns = self.clock.perf_counter_ns()
                        if self.native_dispatch is not None:
                            self.native_dispatch.accept_ingress(venue, symbol, epoch=epoch, payload=payload)
                        self._record("venue_raw", dict(venue=venue, symbol=symbol, epoch=epoch,
                            receiptWallMs=wall, receiptMonoNs=receipt_ns, processingMonoNs=processing_ns, payload=payload))
                        if payload.get("event") == "error" or "code" in payload and payload.get("code") not in (0, "0"):
                            raise ValueError("public subscription error")
                        for event in decode(venue, payload, capture_id=self.runtime.capture_id, symbol=symbol,
                                epoch=epoch, spec=spec, receipt_wall_ms=wall, receipt_ns=receipt_ns, processing_ns=processing_ns):
                            accepted = self.runtime.ingest(event)
                            self._record("venue_event", dict(event=asdict(event), accepted=accepted))
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                self.runtime.gap(venue, symbol, epoch, "cancelled")
                self._record("venue_gap", dict(venue=venue, symbol=symbol, epoch=epoch, reason="cancelled"))
                raise
            except NativeTapeError:
                raise
            except Exception as exc:
                self.runtime.gap(venue, symbol, epoch, "disconnected")
                self._record("venue_gap", dict(venue=venue, symbol=symbol, epoch=epoch,
                    reason="disconnected", errorType=type(exc).__name__))
                attempts += 1
                await asyncio.sleep(min(30, 2**min(attempts, 5)))

    async def close(self):
        self.closed = True
        for task in self.tasks.values():
            task.cancel()
        results = await asyncio.gather(*self.tasks.values(), *self.retired, return_exceptions=True)
        self.tasks.clear()
        self.retired.clear()
        for result in results:
            if isinstance(result, NativeTapeError):
                raise result
