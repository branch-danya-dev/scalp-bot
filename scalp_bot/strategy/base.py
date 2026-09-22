from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..domain import Candle, OrderBook, StrategyDecision, TradeTick, Trend

if TYPE_CHECKING:
    from ..config import Settings
    from .structure import MarketStructure


class Strategy:
    key: str
    label: str
    config: "Settings | None" = None

    def configure(self, config: "Settings") -> None:
        self.config = config

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
        structure: "MarketStructure | None" = None,
    ) -> StrategyDecision:
        raise NotImplementedError

    def reset(self, symbol: str) -> None:
        return None
