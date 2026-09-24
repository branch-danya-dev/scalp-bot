from __future__ import annotations

from scalp_bot.domain import Action, Candle, OrderBook, Side, Trend
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.flow_context import build_multi_horizon_flow_context
from scalp_bot.strategy.market_context import ExecutionContext, MarketContext
from scalp_bot.strategy.playbook_context import (
    PlaybookKind,
    assess_entry_context,
    breakout_direction_plan,
    continuation_direction_plan,
    rejection_direction_plan,
)
from scalp_bot.strategy.regime import (
    HTFBias,
    HTFBiasSnapshot,
    LocalRegime,
    LocalRegimeSnapshot,
)
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel
from scalp_bot.strategy.trend_structure import TrendStructureStrategy
from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy


def candles(count: int = 80) -> list[Candle]:
    return [
        Candle(
            start_ms=index * 60_000,
            open=100.0,
            high=100.08,
            low=99.92,
            close=100.0,
            volume=100.0,
            turnover=10_000.0,
            confirmed=True,
        )
        for index in range(count)
    ]


def execution_ready() -> ExecutionContext:
    return ExecutionContext(
        book_fresh=True,
        book_synced=True,
        book_age_seconds=0.1,
        candle_fresh=True,
        candle_age_seconds=1.0,
        spread_pct=0.0001,
        best_bid=99.99,
        best_ask=100.01,
        top5_bid_notional_usd=50_000.0,
        top5_ask_notional_usd=50_000.0,
        top5_depth_usd=100_000.0,
        trade_buffer_seconds=60.0,
    )


def local_snapshot(
    regime: LocalRegime,
    *,
    direction: Trend,
    parent: Trend,
) -> LocalRegimeSnapshot:
    return LocalRegimeSnapshot(
        regime=regime,
        direction=direction,
        parent_direction=parent,
        strength=0.8,
        structure_1m=direction,
        structure_5m=parent,
        recent_move_pct=0.003 if direction == Trend.UP else -0.003,
        recent_range_pct=0.001,
        baseline_range_pct=0.0008,
        range_expansion_ratio=1.4,
        directional_efficiency=0.7,
        impulse_threshold_pct=0.0015,
        reasons=["test"],
    )


def context(
    regime: LocalRegime,
    *,
    direction: Trend,
    parent: Trend,
    legacy: Trend = Trend.FLAT,
    htf_bias: HTFBias = HTFBias.NEUTRAL,
    flow=None,
) -> MarketContext:
    return MarketContext(
        symbol="AAAUSDT",
        observed_at_ms=100_000,
        last_price=100.0,
        legacy_trend=legacy,
        htf_bias=HTFBiasSnapshot(
            bias=htf_bias,
            strength=0.8,
            trend_15m=legacy,
            trend_1h=Trend.FLAT,
            alignment="test",
            legacy_trend=legacy,
        ),
        local_regime=local_snapshot(
            regime,
            direction=direction,
            parent=parent,
        ),
        flow=flow,
        liquidity=None,
        structure=None,
        execution=execution_ready(),
    )


def short_term_bullish_reversal_flow():
    return build_multi_horizon_flow_context(
        {
            "imbalance5s": 0.85,
            "imbalance15s": -0.45,
            "imbalance60s": -0.60,
            "cvd5s": 8_500.0,
            "cvd15s": -13_500.0,
            "cvd60s": -60_000.0,
            "notional5s": 10_000.0,
            "notional15s": 30_000.0,
            "notional60s": 100_000.0,
            "tradeCount5s": 10,
            "tradeCount15s": 30,
            "tradeCount60s": 100,
        },
        {
            "bestLevelOfiUsd5s": 8_000.0,
            "bestLevelOfiUsd15s": -7_000.0,
            "bestLevelOfiUsd60s": -9_000.0,
            "normalizedOfi5s": 0.08,
            "normalizedOfi15s": -0.07,
            "normalizedOfi60s": -0.09,
            "eventCount5s": 5,
            "eventCount15s": 12,
            "eventCount60s": 30,
        },
        observed_at_ms=100_000,
    )




def strongly_bearish_flow():
    return build_multi_horizon_flow_context(
        {
            "imbalance5s": -0.70,
            "imbalance15s": -0.55,
            "imbalance60s": -0.45,
            "cvd5s": -7_000.0,
            "cvd15s": -16_500.0,
            "cvd60s": -45_000.0,
            "notional5s": 10_000.0,
            "notional15s": 30_000.0,
            "notional60s": 100_000.0,
            "tradeCount5s": 10,
            "tradeCount15s": 30,
            "tradeCount60s": 100,
        },
        {
            "bestLevelOfiUsd5s": -8_000.0,
            "bestLevelOfiUsd15s": -9_000.0,
            "bestLevelOfiUsd60s": -10_000.0,
            "normalizedOfi5s": -0.08,
            "normalizedOfi15s": -0.09,
            "normalizedOfi60s": -0.10,
            "eventCount5s": 5,
            "eventCount15s": 12,
            "eventCount60s": 30,
        },
        observed_at_ms=100_000,
    )

def test_continuation_uses_local_trend_when_legacy_htf_is_flat() -> None:
    ctx = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
        legacy=Trend.FLAT,
    )

    plan = continuation_direction_plan(ctx, Trend.FLAT)

    assert plan.primary_direction == Trend.UP
    assert plan.allowed_directions == (Trend.UP,)
    assert plan.source == "local_regime"


def test_continuation_pullback_inherits_parent_direction() -> None:
    ctx = context(
        LocalRegime.PULLBACK,
        direction=Trend.DOWN,
        parent=Trend.UP,
    )

    plan = continuation_direction_plan(ctx, Trend.FLAT)

    assert plan.primary_direction == Trend.UP


def test_trend_entry_blocks_short_term_bounce_against_longer_flow() -> None:
    ctx = context(
        LocalRegime.PULLBACK,
        direction=Trend.DOWN,
        parent=Trend.UP,
        flow=short_term_bullish_reversal_flow(),
    )

    assessment = assess_entry_context(
        PlaybookKind.TREND_CONTINUATION,
        Action.LONG,
        ctx,
        Trend.FLAT,
    )

    assert assessment.allowed is False
    assert "continuation_flow_short_term_reversal" in assessment.blockers


def test_htf_bearish_bias_does_not_veto_bullish_local_continuation() -> None:
    ctx = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
        legacy=Trend.FLAT,
        htf_bias=HTFBias.BEARISH,
    )

    plan = continuation_direction_plan(ctx, Trend.FLAT)

    assert plan.primary_direction == Trend.UP
    assert any("context-only" in reason for reason in plan.reasons)


def test_breakout_range_is_two_sided_even_when_legacy_htf_is_flat() -> None:
    ctx = context(
        LocalRegime.RANGE,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    plan = breakout_direction_plan(ctx, Trend.FLAT)

    assert set(plan.allowed_directions) == {Trend.UP, Trend.DOWN}
    assert plan.source == "range_two_sided"


def test_rejection_range_is_two_sided() -> None:
    ctx = context(
        LocalRegime.RANGE,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    plan = rejection_direction_plan(ctx, Trend.FLAT)

    assert set(plan.allowed_directions) == {Trend.UP, Trend.DOWN}


def test_rejection_transition_is_two_sided_with_directional_preference() -> None:
    ctx = context(
        LocalRegime.TRANSITION,
        direction=Trend.DOWN,
        parent=Trend.UP,
    )

    plan = rejection_direction_plan(ctx, Trend.UP)

    assert plan.primary_direction == Trend.DOWN
    assert set(plan.allowed_directions) == {
        Trend.DOWN,
        Trend.UP,
    }
    assert plan.source == "transition_preference_two_sided"


def test_trend_strategy_is_not_disabled_by_legacy_flat_when_local_is_bullish() -> None:
    strategy = TrendStructureStrategy()
    ctx = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
    )

    decision = strategy.evaluate(
        candles(),
        OrderBook(
            bids=[(99.99, 100.0)],
            asks=[(100.01, 100.0)],
        ),
        Trend.FLAT,
        symbol="AAAUSDT",
        trades=[],
        structure=MarketStructure(),
        market_context=ctx,
        observed_at_ms=100_000,
    )

    assert "MarketContext пока не даёт" not in decision.reasons[0]
    assert decision.details["state"] == "search"


def test_breakout_strategy_enters_search_under_range_instead_of_htf_flat_veto() -> None:
    strategy = LevelBreakoutStrategy()
    ctx = context(
        LocalRegime.RANGE,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    decision = strategy.evaluate(
        candles(),
        OrderBook(
            bids=[(99.99, 100.0)],
            asks=[(100.01, 100.0)],
        ),
        Trend.FLAT,
        symbol="AAAUSDT",
        trades=[],
        structure=MarketStructure(),
        market_context=ctx,
        observed_at_ms=100_000,
    )

    assert "MarketContext пока не даёт направление" not in decision.reasons[0]
    assert set(
        decision.details["playbookContext"]["allowedDirections"]
    ) == {"up", "down"}


def test_rejection_strategy_searches_levels_in_range_with_legacy_flat() -> None:
    strategy = WeakLevelRejectionStrategy()
    ctx = context(
        LocalRegime.RANGE,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    decision = strategy.evaluate(
        candles(),
        OrderBook(
            bids=[(99.99, 100.0)],
            asks=[(100.01, 100.0)],
        ),
        Trend.FLAT,
        symbol="AAAUSDT",
        trades=[],
        structure=MarketStructure(),
        market_context=ctx,
        observed_at_ms=100_000,
    )

    assert "MarketContext пока не разрешает" not in decision.reasons[0]
    assert decision.details["state"] == "search"


def test_trend_position_management_uses_local_context_not_legacy_flat() -> None:
    strategy = TrendStructureStrategy()
    bullish = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
        legacy=Trend.FLAT,
    )
    bearish = context(
        LocalRegime.BEARISH_IMPULSE,
        direction=Trend.DOWN,
        parent=Trend.DOWN,
        legacy=Trend.FLAT,
    )

    assert strategy.manage_position(
        side=Side.LONG,
        unrealized_pnl=-1.0,
        opened_at=0.0,
        strategy_details={},
        decision=None,
        trend=Trend.FLAT,
        last_price=100.0,
        market_context=bullish,
    ) is None

    assert strategy.manage_position(
        side=Side.LONG,
        unrealized_pnl=-1.0,
        opened_at=0.0,
        strategy_details={},
        decision=None,
        trend=Trend.FLAT,
        last_price=100.0,
        market_context=bearish,
    ) == "trend_structure_context_lost"



def test_breakout_bearish_regime_is_preference_not_short_only() -> None:
    ctx = context(
        LocalRegime.BEARISH_IMPULSE,
        direction=Trend.DOWN,
        parent=Trend.DOWN,
    )

    plan = breakout_direction_plan(ctx, Trend.DOWN)

    assert plan.primary_direction == Trend.DOWN
    assert set(plan.allowed_directions) == {
        Trend.DOWN,
        Trend.UP,
    }
    assert plan.source == "local_regime_preference_two_sided"


def test_rejection_bullish_regime_is_preference_not_long_only() -> None:
    ctx = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
    )

    plan = rejection_direction_plan(ctx, Trend.UP)

    assert plan.primary_direction == Trend.UP
    assert set(plan.allowed_directions) == {
        Trend.UP,
        Trend.DOWN,
    }
    assert plan.source == "local_regime_preference_two_sided"


def test_breakout_unclear_regime_allows_evidence_to_choose_direction() -> None:
    ctx = context(
        LocalRegime.UNCLEAR,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    plan = breakout_direction_plan(ctx, Trend.FLAT)

    assert set(plan.allowed_directions) == {
        Trend.UP,
        Trend.DOWN,
    }
    assert plan.primary_direction == Trend.FLAT
    assert plan.source == "unclear_two_sided"


def test_rejection_unclear_regime_allows_failed_break_evidence() -> None:
    ctx = context(
        LocalRegime.UNCLEAR,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    plan = rejection_direction_plan(ctx, Trend.FLAT)

    assert set(plan.allowed_directions) == {
        Trend.UP,
        Trend.DOWN,
    }
    assert plan.primary_direction == Trend.FLAT
    assert plan.source == "unclear_two_sided"


def test_symmetric_range_has_no_fake_primary_direction() -> None:
    ctx = context(
        LocalRegime.RANGE,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
    )

    assert breakout_direction_plan(
        ctx,
        Trend.FLAT,
    ).primary_direction == Trend.FLAT
    assert rejection_direction_plan(
        ctx,
        Trend.FLAT,
    ).primary_direction == Trend.FLAT


def _worked_level(
    kind: str,
    center: float,
    generation: str,
) -> StructuralLevel:
    return StructuralLevel(
        kind=kind,
        low=center - 0.03,
        high=center + 0.03,
        touches=6,
        timeframe="1m",
        score=0.8,
        reaction_pct=0.005,
        volume_ratio=1.2,
        last_touch_index=70,
        level_id=generation.rsplit(":g", 1)[0],
        generation_id=generation,
        distinct_approaches=5,
        dwell_bars=5,
        acceptance_bars=1,
        lifecycle="worked",
    )


def _young_level(
    kind: str,
    center: float,
    generation: str,
) -> StructuralLevel:
    return StructuralLevel(
        kind=kind,
        low=center - 0.02,
        high=center + 0.02,
        touches=2,
        timeframe="1m",
        score=0.6,
        reaction_pct=0.003,
        volume_ratio=1.0,
        last_touch_index=70,
        level_id=generation.rsplit(":g", 1)[0],
        generation_id=generation,
        distinct_approaches=2,
        dwell_bars=1,
        acceptance_bars=1,
        lifecycle="tested",
    )


def test_breakout_prefers_near_primary_direction_level_over_closer_opposite() -> None:
    strategy = LevelBreakoutStrategy()
    ctx = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
    )
    structure = MarketStructure(
        levels=[
            _worked_level(
                "support",
                99.95,
                "S:support:1:g1",
            ),
            _worked_level(
                "resistance",
                100.20,
                "R:resistance:1:g1",
            ),
        ]
    )

    decision = strategy.evaluate(
        candles(),
        OrderBook(
            bids=[(99.99, 100.0)],
            asks=[(100.01, 100.0)],
        ),
        Trend.FLAT,
        symbol="PRIMARYBREAKUSDT",
        trades=[],
        structure=structure,
        market_context=ctx,
        observed_at_ms=100_000,
    )

    assert decision.details["playbookTrend"] == "up"
    assert decision.details["zone"]["kind"] == "resistance"


def test_rejection_prefers_near_primary_direction_level_over_closer_opposite() -> None:
    strategy = WeakLevelRejectionStrategy()
    ctx = context(
        LocalRegime.BULLISH_TREND,
        direction=Trend.UP,
        parent=Trend.UP,
    )
    structure = MarketStructure(
        levels=[
            _young_level(
                "support",
                99.90,
                "S:support:2:g1",
            ),
            _young_level(
                "resistance",
                100.02,
                "R:resistance:2:g1",
            ),
        ]
    )

    decision = strategy.evaluate(
        candles(),
        OrderBook(
            bids=[(99.99, 100.0)],
            asks=[(100.01, 100.0)],
        ),
        Trend.FLAT,
        symbol="PRIMARYREJECTUSDT",
        trades=[],
        structure=structure,
        market_context=ctx,
        observed_at_ms=100_000,
    )

    assert decision.details["zone"]["kind"] == "support"


def test_rejection_still_blocks_multi_horizon_opposed_flow_in_unclear_context() -> None:
    ctx = context(
        LocalRegime.UNCLEAR,
        direction=Trend.FLAT,
        parent=Trend.FLAT,
        flow=strongly_bearish_flow(),
    )

    assessment = assess_entry_context(
        PlaybookKind.LEVEL_REJECTION,
        Action.LONG,
        ctx,
        Trend.FLAT,
    )

    assert assessment.allowed is False
    assert "rejection_flow_opposed" in assessment.blockers
