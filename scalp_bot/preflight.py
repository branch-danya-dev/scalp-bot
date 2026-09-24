from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .bybit import MarketMessage, OrderBookState, stream_symbol
from .config import Settings


@dataclass(slots=True)
class RuntimeMarketProbe:
    symbol: str
    fast_depth: int
    deep_depth: int
    fast_state: OrderBookState = field(init=False)
    deep_state: OrderBookState = field(init=False)
    saw_kline: bool = False
    saw_trade: bool = False
    _ready: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
    )

    def __post_init__(self) -> None:
        self.fast_state = OrderBookState(self.fast_depth)
        self.deep_state = OrderBookState(self.deep_depth)

    @property
    def fast_synced(self) -> bool:
        return self.fast_state.synced

    @property
    def deep_synced(self) -> bool:
        if self.fast_depth == self.deep_depth:
            return self.fast_state.synced
        return self.deep_state.synced

    @property
    def ready(self) -> bool:
        return (
            self.fast_synced
            and self.deep_synced
            and self.saw_kline
            and self.saw_trade
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "fastDepth": self.fast_depth,
            "deepDepth": self.deep_depth,
            "fastBookSynced": self.fast_synced,
            "deepBookSynced": self.deep_synced,
            "fastSeq": self.fast_state.last_seq,
            "deepSeq": (
                self.fast_state.last_seq
                if self.fast_depth == self.deep_depth
                else self.deep_state.last_seq
            ),
            "sawKline1m": self.saw_kline,
            "sawPublicTrade": self.saw_trade,
            "ready": self.ready,
        }

    async def on_message(
        self,
        message: MarketMessage,
    ) -> None:
        topic = str(message.topic or "")
        fast_topic = (
            f"orderbook.{self.fast_depth}.{self.symbol}"
        )
        deep_topic = (
            f"orderbook.{self.deep_depth}.{self.symbol}"
        )

        if topic == fast_topic:
            self.fast_state.apply(message)
            if self.fast_depth == self.deep_depth:
                self.deep_state = self.fast_state
        elif topic == deep_topic:
            self.deep_state.apply(message)
        elif topic == f"kline.1.{self.symbol}":
            if message.get("data"):
                self.saw_kline = True
        elif topic == f"publicTrade.{self.symbol}":
            if message.get("data"):
                self.saw_trade = True

        if self.ready:
            self._ready.set()

    async def wait_ready(
        self,
        timeout_seconds: float,
    ) -> None:
        await asyncio.wait_for(
            self._ready.wait(),
            timeout=max(0.1, timeout_seconds),
        )


async def probe_runtime_market(
    settings: Settings,
    symbol: str,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    probe = RuntimeMarketProbe(
        symbol=symbol,
        fast_depth=settings.fast_orderbook_depth,
        deep_depth=settings.deep_orderbook_depth,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        stream_symbol(
            settings.bybit_public_ws_url,
            symbol,
            probe.on_message,
            stop_event,
            fast_orderbook_depth=(
                settings.fast_orderbook_depth
            ),
            deep_orderbook_depth=(
                settings.deep_orderbook_depth
            ),
            market_queue_size=settings.market_queue_size,
            market_queue_put_timeout_seconds=(
                settings.market_queue_put_timeout_seconds
            ),
            market_queue_max_lag_seconds=(
                settings.market_queue_max_lag_seconds
            ),
        ),
        name=f"market-preflight-{symbol}",
    )
    timeout = (
        settings.market_preflight_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    try:
        await probe.wait_ready(timeout)
    except TimeoutError as exc:
        raise RuntimeError(
            "runtime market preflight timed out: "
            f"{probe.diagnostics()}"
        ) from exc
    finally:
        stop_event.set()
        task.cancel()
        await asyncio.gather(
            task,
            return_exceptions=True,
        )
    return probe.diagnostics()
