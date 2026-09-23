from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain import Action, OrderBook, Trend
from .flow_context import MultiHorizonFlowContext
from .liquidity_evidence import LiquidityEvidence
from .regime import HTFBiasSnapshot, LocalRegimeSnapshot
from .structure import MarketStructure, StructuralLevel, TrendLine


@dataclass(frozen=True, slots=True)
class StructureContext:
    reference_price: float
    level_count: int
    trendline_count: int
    nearest_support: StructuralLevel | None
    nearest_resistance: StructuralLevel | None
    support_distance_pct: float | None
    resistance_distance_pct: float | None
    support_trendline: TrendLine | None
    resistance_trendline: TrendLine | None

    def public(self) -> dict[str, Any]:
        return {
            "referencePrice": self.reference_price,
            "levelCount": self.level_count,
            "trendlineCount": self.trendline_count,
            "nearestSupport": (
                self.nearest_support.public()
                if self.nearest_support is not None
                else None
            ),
            "nearestResistance": (
                self.nearest_resistance.public()
                if self.nearest_resistance is not None
                else None
            ),
            "supportDistancePct": self.support_distance_pct,
            "resistanceDistancePct": self.resistance_distance_pct,
            "supportTrendline": (
                self.support_trendline.public()
                if self.support_trendline is not None
                else None
            ),
            "resistanceTrendline": (
                self.resistance_trendline.public()
                if self.resistance_trendline is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    book_fresh: bool
    book_synced: bool | None
    book_age_seconds: float | None
    candle_fresh: bool
    candle_age_seconds: float | None
    spread_pct: float
    best_bid: float | None
    best_ask: float | None
    top5_bid_notional_usd: float
    top5_ask_notional_usd: float
    top5_depth_usd: float
    trade_buffer_seconds: float

    @property
    def ready(self) -> bool:
        return self.book_fresh and self.candle_fresh

    def public(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "bookFresh": self.book_fresh,
            "bookSynced": self.book_synced,
            "bookAgeSeconds": self.book_age_seconds,
            "candleFresh": self.candle_fresh,
            "candleAgeSeconds": self.candle_age_seconds,
            "spreadPct": self.spread_pct,
            "bestBid": self.best_bid,
            "bestAsk": self.best_ask,
            "top5BidNotionalUsd": self.top5_bid_notional_usd,
            "top5AskNotionalUsd": self.top5_ask_notional_usd,
            "top5DepthUsd": self.top5_depth_usd,
            "tradeBufferSeconds": self.trade_buffer_seconds,
        }


@dataclass(frozen=True, slots=True)
class MarketContext:
    symbol: str
    observed_at_ms: int
    last_price: float
    legacy_trend: Trend
    htf_bias: HTFBiasSnapshot | None
    local_regime: LocalRegimeSnapshot | None
    flow: MultiHorizonFlowContext | None
    liquidity: LiquidityEvidence | None
    structure: StructureContext | None
    execution: ExecutionContext

    def flow_alignment_for(self, action: Action):
        if self.flow is None:
            return None
        return self.flow.alignment_for(action)

    def liquidity_alignment_for(self, action: Action):
        if self.liquidity is None:
            return None
        return self.liquidity.alignment_for(action)

    def fingerprint(self) -> tuple:
        return (
            self.legacy_trend.value,
            (
                self.htf_bias.bias.value
                if self.htf_bias is not None
                else None
            ),
            (
                self.htf_bias.alignment
                if self.htf_bias is not None
                else None
            ),
            (
                self.local_regime.regime.value
                if self.local_regime is not None
                else None
            ),
            (
                self.local_regime.direction.value
                if self.local_regime is not None
                else None
            ),
            (
                self.flow.dominant_direction.value
                if self.flow is not None
                else None
            ),
            (
                self.flow.long_alignment.classification.value
                if self.flow is not None
                else None
            ),
            (
                self.flow.short_alignment.classification.value
                if self.flow is not None
                else None
            ),
            (
                self.liquidity.state.value
                if self.liquidity is not None
                else None
            ),
            (
                self.liquidity.directional_bias.value
                if self.liquidity is not None
                else None
            ),
            self.execution.book_fresh,
            self.execution.candle_fresh,
        )

    def public(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "symbol": self.symbol,
            "observedAtMs": self.observed_at_ms,
            "lastPrice": self.last_price,
            "legacyTrend": self.legacy_trend.value,
            "htfBias": (
                self.htf_bias.public()
                if self.htf_bias is not None
                else None
            ),
            "localRegime": (
                self.local_regime.public()
                if self.local_regime is not None
                else None
            ),
            "flowContext": (
                self.flow.public()
                if self.flow is not None
                else None
            ),
            "liquidityEvidence": (
                self.liquidity.public()
                if self.liquidity is not None
                else None
            ),
            "structureContext": (
                self.structure.public()
                if self.structure is not None
                else None
            ),
            "executionContext": self.execution.public(),
        }


def build_structure_context(
    structure: MarketStructure | None,
    reference_price: float,
) -> StructureContext | None:
    if structure is None or reference_price <= 0:
        return None

    supports = [
        level
        for level in structure.levels
        if level.kind == "support"
        and level.center <= reference_price
    ]
    resistances = [
        level
        for level in structure.levels
        if level.kind == "resistance"
        and level.center >= reference_price
    ]
    nearest_support = (
        min(
            supports,
            key=lambda level: (
                abs(reference_price - level.center),
                -level.score,
            ),
        )
        if supports
        else None
    )
    nearest_resistance = (
        min(
            resistances,
            key=lambda level: (
                abs(level.center - reference_price),
                -level.score,
            ),
        )
        if resistances
        else None
    )
    support_distance = (
        abs(reference_price - nearest_support.center)
        / reference_price
        if nearest_support is not None
        else None
    )
    resistance_distance = (
        abs(nearest_resistance.center - reference_price)
        / reference_price
        if nearest_resistance is not None
        else None
    )

    return StructureContext(
        reference_price=reference_price,
        level_count=len(structure.levels),
        trendline_count=len(structure.trendlines),
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        support_distance_pct=support_distance,
        resistance_distance_pct=resistance_distance,
        support_trendline=structure.trendline("support"),
        resistance_trendline=structure.trendline("resistance"),
    )


def build_execution_context(
    *,
    book: OrderBook,
    book_fresh: bool,
    book_synced: bool | None,
    book_age_seconds: float | None,
    candle_fresh: bool,
    candle_age_seconds: float | None,
    trade_buffer_seconds: float,
) -> ExecutionContext:
    top5_bid = sum(
        price * qty
        for price, qty in book.bids[:5]
    )
    top5_ask = sum(
        price * qty
        for price, qty in book.asks[:5]
    )
    return ExecutionContext(
        book_fresh=book_fresh,
        book_synced=book_synced,
        book_age_seconds=book_age_seconds,
        candle_fresh=candle_fresh,
        candle_age_seconds=candle_age_seconds,
        spread_pct=book.spread_pct,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        top5_bid_notional_usd=top5_bid,
        top5_ask_notional_usd=top5_ask,
        top5_depth_usd=top5_bid + top5_ask,
        trade_buffer_seconds=trade_buffer_seconds,
    )
