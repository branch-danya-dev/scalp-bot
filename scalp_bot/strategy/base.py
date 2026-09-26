from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain import Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .structure import MarketStructure


class Strategy:
    key: str
    label: str

    def prepare_plan(self, scenario, decision, book, candles, structure):
        from .preparation import preparation_plan
        return preparation_plan(self, scenario, decision, book, candles, structure)

    def entry_invalidation(self, decision: StrategyDecision, book: OrderBook) -> str | None:
        """The executable exit side must not already contradict the hypothesis.

        This is the same zone used by the owner's management, not a grace timer
        masking an invalid entry. Protective broker stops remain independent.
        """
        price = book.executable_exit(decision.side)
        if price is None:
            return "executable exit side missing"
        zone = decision.details.get("zone") or {}
        if self.key == "level_breakout":
            boundary = zone.get("high" if decision.side==Side.LONG else "low")
        elif self.key == "weak_level_rejection":
            boundary = zone.get("low" if decision.side==Side.LONG else "high")
        else:
            boundary = decision.stop
        if boundary and (price < boundary if decision.side==Side.LONG else price > boundary):
            return "hypothesis already invalid on executable exit quote"
        return None

    def manage_progress(self, config, clock, position, gross_mark_original):
        from .position_policy import should_exit_without_progress
        return should_exit_without_progress(config, clock, position, gross_mark_original)

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
        structure: "MarketStructure | None" = None,
        market_context: "MarketContext | None" = None,
        observed_at_ms: int | None = None,
        trade_flow: dict | None = None,
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
        book: OrderBook | None = None,
        market_context: "MarketContext | None" = None,
        observed_at_ms: int | None = None,
    ) -> str | None:
        return None
