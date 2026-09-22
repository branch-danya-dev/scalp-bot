from __future__ import annotations

from ..domain import Candle, OrderBook, StrategyDecision, TradeTick, Trend


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
    ) -> StrategyDecision:
        raise NotImplementedError

    def reset(self, symbol: str) -> None:
        return None
