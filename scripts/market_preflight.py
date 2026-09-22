from __future__ import annotations

import asyncio
import json
import sys

import websockets

from scalp_bot.config import settings
from scalp_bot.bybit import BybitError, BybitRestClient


async def probe_public_websocket(symbol: str) -> None:
    try:
        async with websockets.connect(
            settings.bybit_public_ws_url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            topic = f"orderbook.1.{symbol}"
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [topic],
            }))
            for _ in range(6):
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                message = json.loads(raw)
                if message.get("topic") == topic:
                    return
    except Exception as exc:
        raise RuntimeError(
            (
                "Bybit public WebSocket preflight failed at "
                f"{settings.bybit_public_ws_url}: "
                f"{type(exc).__name__}: {exc}"
            )
        ) from exc
    raise RuntimeError(
        "Bybit public WebSocket connected but no order-book data arrived"
    )


async def main() -> None:
    client = BybitRestClient(settings)
    try:
        candidates = await client.active_candidates()
        if not candidates:
            raise RuntimeError(
                "Bybit scanner returned zero eligible candidates"
            )
        sample = ", ".join(
            item.symbol
            for item in candidates[:5]
        )
        print(
            "REST preflight passed: "
            f"{client.active_rest_url} · "
            f"{len(candidates)} candidates · top={sample}"
        )
        await probe_public_websocket(candidates[0].symbol)
        print(
            "WebSocket preflight passed: "
            f"{settings.bybit_public_ws_url}"
        )
    except (BybitError, RuntimeError) as exc:
        print("")
        print("MARKET PREFLIGHT FAILED")
        print(str(exc))
        print("")
        print(
            "The research run was not started because its configured "
            "Bybit Global linear market is not reachable from this "
            "connection."
        )
        raise SystemExit(2) from None
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
