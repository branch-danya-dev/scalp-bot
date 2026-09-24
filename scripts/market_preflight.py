from __future__ import annotations

import asyncio
import json
import sys
from time import monotonic

import websockets

from scalp_bot.config import settings
from scalp_bot.bybit import (
    BybitError,
    BybitRestClient,
    OrderBookState,
    runtime_stream_topics,
)


async def _probe_topic_group(
    symbol: str,
    topics: list[str],
    *,
    timeout_seconds: float = 20.0,
) -> set[str]:
    if not topics:
        return set()

    orderbooks: dict[str, OrderBookState] = {}
    for topic in topics:
        if topic.startswith("orderbook."):
            depth = int(topic.split(".")[1])
            orderbooks[topic] = OrderBookState(depth)

    received: set[str] = set()
    deadline = monotonic() + timeout_seconds
    async with websockets.connect(
        settings.bybit_public_ws_url,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": topics,
        }))
        while monotonic() < deadline:
            remaining = max(0.1, deadline - monotonic())
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=min(5.0, remaining),
            )
            message = json.loads(raw)
            topic = str(message.get("topic") or "")
            if topic not in topics:
                continue
            state = orderbooks.get(topic)
            if state is not None:
                state.apply(message)
                if not state.synced:
                    continue
            received.add(topic)
            if received.issuperset(topics):
                return received
    return received


async def probe_public_websocket(symbol: str) -> None:
    fast_topics, deep_topics = runtime_stream_topics(
        symbol,
        fast_orderbook_depth=settings.fast_orderbook_depth,
        deep_orderbook_depth=settings.deep_orderbook_depth,
    )
    try:
        fast_received, deep_received = await asyncio.gather(
            _probe_topic_group(symbol, fast_topics),
            _probe_topic_group(symbol, deep_topics),
        )
    except Exception as exc:
        raise RuntimeError(
            (
                "Bybit public WebSocket preflight failed at "
                f"{settings.bybit_public_ws_url}: "
                f"{type(exc).__name__}: {exc}"
            )
        ) from exc

    expected = set(fast_topics + deep_topics)
    received = set(fast_received) | set(deep_received)
    missing = sorted(expected - received)
    if missing:
        raise RuntimeError(
            "Bybit runtime WebSocket preflight did not receive: "
            + ", ".join(missing)
        )

    print(
        "WebSocket runtime topics passed: "
        + ", ".join(sorted(received))
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
            "Bybit Global linear runtime feeds are not ready."
        )
        raise SystemExit(2) from None
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
