from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain import Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend

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

    def mark_opened(
        self,
        symbol: str,
        decision: StrategyDecision,
    ) -> None:
        return None

    def manage_position(
        self,
        *,
        side: Side,
        unrealized_pnl: float,
        opened_at: float,
        strategy_details: dict,
        decision: StrategyDecision | None,
        trend: Trend,
        last_price: float,
    ) -> str | None:
        return None
