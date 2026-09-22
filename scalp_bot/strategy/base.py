from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain import Candle, OrderBook, StrategyDecision, TradeTick, Trend

if TYPE_CHECKING:
    from .structure import MarketStructure


class Strategy:
    key: str
    label: str

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
