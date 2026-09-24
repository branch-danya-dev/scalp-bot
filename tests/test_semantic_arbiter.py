from __future__ import annotations

import pytest

from scalp_bot.domain import Action, Side, StrategyDecision, TradePlan, Trend
from scalp_bot.strategy.market_context import (
    ExecutionContext,
    MarketContext,
    StructureContext,
)
from scalp_bot.strategy.semantic_arbiter import (
    assess_candidate,
    assess_session_candidates,
    assess_structural_path,
    build_selection_priority,
)
from scalp_bot.strategy.structure import StructuralLevel


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


def mature_level(
    kind: str,
    low: float,
    high: float,
    *,
    generation: str,
) -> StructuralLevel:
    return StructuralLevel(
        kind=kind,
        low=low,
        high=high,
        touches=4,
        timeframe="5m",
        score=0.9,
        reaction_pct=0.004,
        volume_ratio=1.2,
        generation_id=generation,
        distinct_approaches=4,
        dwell_bars=4,
        acceptance_bars=1,
        lifecycle="worked",
    )


def context(
    *,
    support: StructuralLevel | None = None,
    resistance: StructuralLevel | None = None,
) -> MarketContext:
    structure = StructureContext(
        reference_price=100.0,
        level_count=int(support is not None) + int(resistance is not None),
        trendline_count=0,
        nearest_support=support,
        nearest_resistance=resistance,
        support_distance_pct=(
            abs(100.0 - support.center) / 100.0
            if support is not None
            else None
        ),
        resistance_distance_pct=(
            abs(resistance.center - 100.0) / 100.0
            if resistance is not None
            else None
        ),
        support_trendline=None,
        resistance_trendline=None,
    )
    return MarketContext(
        symbol="AAAUSDT",
        observed_at_ms=100_000,
        last_price=100.0,
        legacy_trend=Trend.FLAT,
        htf_bias=None,
        local_regime=None,
        flow=None,
        liquidity=None,
        structure=structure,
        execution=execution_ready(),
    )


def decision(
    strategy: str,
    action: Action,
    *,
    entry: float = 100.0,
    stop: float | None = None,
    target: float | None = None,
    watched_level: float = 100.0,
    details: dict | None = None,
) -> StrategyDecision:
    if stop is None:
        stop = 99.5 if action == Action.LONG else 100.5
    if target is None:
        target = 101.0 if action == Action.LONG else 99.0
    return StrategyDecision(
        strategy=strategy,
        action=action,
        reasons=["ready"],
        confidence=0.99,
        watched_level=watched_level,
        entry=entry,
        stop=stop,
        target=target,
        details=details or {},
        setup_id=f"{strategy}:{action.value}:test",
    )


def plan(
    strategy: str,
    side: Side,
    *,
    setup_id: str | None = None,
    net_rr: float = 1.0,
    drift: float = 0.0,
) -> TradePlan:
    return TradePlan(
        symbol="AAAUSDT",
        strategy=strategy,
        side=side,
        setup_entry=100.0,
        market_entry=100.0,
        stop=99.5 if side == Side.LONG else 100.5,
        target=101.0 if side == Side.LONG else 99.0,
        notional=1000.0,
        leverage=1.0,
        max_loss_usd=5.0,
        expected_gross_profit=10.0,
        estimated_costs=1.0,
        expected_net_profit=9.0,
        expected_net_loss=5.0,
        net_reward_risk=net_rr,
        entry_drift_pct=drift,
        setup_id=setup_id or f"{strategy}:{side.value}:test",
    )


def test_trend_long_is_blocked_by_mature_resistance_before_first_take() -> None:
    resistance = mature_level(
        "resistance",
        100.20,
        100.30,
        generation="R:g1",
    )
    ctx = context(resistance=resistance)
    trade = decision(
        "trend_structure",
        Action.LONG,
        stop=99.50,
        target=101.20,
    )

    assessment = assess_candidate(
        trade,
        ctx,
        partial_take_at_r=1.0,
        partial_take_enabled=True,
    )

    assert assessment.allowed is False
    assert (
        "mature_structural_obstacle_before_first_take"
        in assessment.blockers
    )
    assert assessment.structural_path.first_take_price == 100.50
    assert assessment.structural_path.obstacle_before_first_take is True


def test_short_is_blocked_by_mature_support_before_first_take() -> None:
    support = mature_level(
        "support",
        99.70,
        99.80,
        generation="S:g1",
    )
    ctx = context(support=support)
    trade = decision(
        "weak_level_rejection",
        Action.SHORT,
        stop=100.50,
        target=98.80,
    )

    assessment = assess_candidate(trade, ctx)

    assert assessment.allowed is False
    assert (
        "mature_structural_obstacle_before_first_take"
        in assessment.blockers
    )
    assert assessment.structural_path.first_take_price == 99.50


def test_breakout_own_accepted_level_is_exempt_from_structural_veto() -> None:
    resistance = mature_level(
        "resistance",
        99.95,
        100.10,
        generation="R:g1",
    )
    ctx = context(resistance=resistance)
    trade = decision(
        "level_breakout",
        Action.LONG,
        entry=100.05,
        stop=99.70,
        target=101.0,
        watched_level=100.0,
        details={
            "state": "impulse",
            "zone": {
                "kind": "resistance",
                "low": 99.95,
                "high": 100.10,
            },
        },
    )

    path = assess_structural_path(trade, ctx)

    assert path.blocked is False
    assert path.own_breakout_level_exempted is True


def test_breakout_next_mature_level_reduces_risk_instead_of_binary_veto() -> None:
    resistance = mature_level(
        "resistance",
        100.20,
        100.30,
        generation="R:next",
    )
    ctx = context(resistance=resistance)
    trade = decision(
        "level_breakout",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
        watched_level=99.80,
        details={
            "state": "impulse",
            "zone": {
                "kind": "resistance",
                "low": 99.70,
                "high": 99.90,
            },
            "opportunityFreshness": {
                "classification": "fresh",
            },
            "flowAlignment": {
                "classification": "strongly_aligned",
            },
            "liquidityAlignment": {
                "classification": "supportive",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert assessment.allowed is True
    assert assessment.structural_path.own_breakout_level_exempted is False
    assert assessment.structural_path.obstacle_before_first_take is True
    assert assessment.structural_path.risk_scale == 0.65
    assert assessment.risk_scale == 0.65


def test_breakout_obstacle_stays_blocked_without_consumption_strength() -> None:
    resistance = mature_level(
        "resistance",
        100.20,
        100.30,
        generation="R:next",
    )
    ctx = context(resistance=resistance)
    trade = decision(
        "level_breakout",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
        watched_level=99.80,
        details={
            "state": "impulse",
            "zone": {
                "kind": "resistance",
                "low": 99.70,
                "high": 99.90,
            },
            "opportunityFreshness": {
                "classification": "late",
            },
            "flowAlignment": {
                "classification": "mixed",
            },
            "liquidityAlignment": {
                "classification": "neutral",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert assessment.allowed is False
    assert (
        "managed_structural_obstacle_requires_breakout_strength"
        in assessment.blockers
    )


def test_exhausted_opportunity_is_blocked_even_when_other_semantics_are_good() -> None:
    ctx = context()
    trade = decision(
        "level_breakout",
        Action.LONG,
        details={
            "opportunityFreshness": {
                "classification": "exhausted",
            },
            "flowAlignment": {
                "classification": "strongly_aligned",
            },
            "liquidityAlignment": {
                "classification": "supportive",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert assessment.allowed is False
    assert "opportunity_exhausted" in assessment.blockers


def test_fresh_high_quality_breakout_gets_bounded_risk_scale() -> None:
    ctx = context()
    trade = decision(
        "level_breakout",
        Action.LONG,
        details={
            "opportunityFreshness": {
                "classification": "fresh",
            },
            "flowAlignment": {
                "classification": "strongly_aligned",
            },
            "liquidityAlignment": {
                "classification": "supportive",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert assessment.allowed is True
    assert assessment.risk_scale == 1.0
    assert any(
        "positive risk scaling is disabled"
        in reason
        for reason in assessment.reasons
    )


def test_trend_strategy_never_receives_aggressive_risk_scale() -> None:
    ctx = context()
    trade = decision(
        "trend_structure",
        Action.LONG,
        details={
            "opportunityFreshness": {
                "classification": "fresh",
            },
            "flowAlignment": {
                "classification": "strongly_aligned",
            },
            "liquidityAlignment": {
                "classification": "supportive",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert assessment.allowed is True
    assert assessment.risk_scale == 1.0


def test_opposite_tradeable_playbooks_at_same_location_block_each_other() -> None:
    ctx = context()
    trend_long = decision(
        "trend_structure",
        Action.LONG,
        watched_level=100.0,
    )
    rejection_short = decision(
        "weak_level_rejection",
        Action.SHORT,
        watched_level=100.1,
    )

    result = assess_session_candidates(
        [trend_long, rejection_short],
        ctx,
    )

    assert result["trend_structure"].allowed is False
    assert result["weak_level_rejection"].allowed is False
    assert (
        "opposing_playbook_conflict"
        in result["trend_structure"].blockers
    )
    assert result["trend_structure"].conflicting_strategies == (
        "weak_level_rejection",
    )


def test_same_direction_playbooks_create_confluence_not_competition() -> None:
    ctx = context()
    trend_long = decision(
        "trend_structure",
        Action.LONG,
        watched_level=100.0,
    )
    breakout_long = decision(
        "level_breakout",
        Action.LONG,
        watched_level=100.1,
    )

    result = assess_session_candidates(
        [trend_long, breakout_long],
        ctx,
    )

    assert result["trend_structure"].allowed is True
    assert result["level_breakout"].allowed is True
    assert result["trend_structure"].confluence_count == 1
    assert result["level_breakout"].confluence_count == 1


def test_setup_quality_does_not_change_shared_selection_priority() -> None:
    ctx = context()
    low_quality = decision(
        "trend_structure",
        Action.LONG,
        details={
            "setupQuality": 0.10,
            "entryFreshness": {"classification": "fresh"},
            "flowAlignment": {"classification": "aligned"},
            "liquidityAlignment": {"classification": "neutral"},
        },
    )
    high_quality = decision(
        "trend_structure",
        Action.LONG,
        details={
            "setupQuality": 0.99,
            "entryFreshness": {"classification": "fresh"},
            "flowAlignment": {"classification": "aligned"},
            "liquidityAlignment": {"classification": "neutral"},
        },
    )

    low_assessment = assess_candidate(low_quality, ctx)
    high_assessment = assess_candidate(high_quality, ctx)
    shared_plan = plan(
        "trend_structure",
        Side.LONG,
        setup_id=low_quality.setup_id,
        net_rr=1.2,
    )

    low_priority = build_selection_priority(
        low_assessment,
        shared_plan,
        activity_rank=5,
        activity_score=50,
    )
    high_priority = build_selection_priority(
        high_assessment,
        shared_plan,
        activity_rank=5,
        activity_score=50,
    )

    assert low_priority.key() == high_priority.key()


def test_fresh_entry_outranks_exhausted_when_semantics_and_economics_match() -> None:
    ctx = context()
    fresh = decision(
        "trend_structure",
        Action.LONG,
        details={
            "entryFreshness": {"classification": "fresh"},
            "flowAlignment": {"classification": "aligned"},
            "liquidityAlignment": {"classification": "neutral"},
        },
    )
    exhausted = decision(
        "trend_structure",
        Action.LONG,
        details={
            "entryFreshness": {"classification": "exhausted"},
            "flowAlignment": {"classification": "aligned"},
            "liquidityAlignment": {"classification": "neutral"},
        },
    )

    fresh_priority = build_selection_priority(
        assess_candidate(fresh, ctx),
        plan("trend_structure", Side.LONG, setup_id=fresh.setup_id),
        activity_rank=5,
        activity_score=50,
    )
    exhausted_priority = build_selection_priority(
        assess_candidate(exhausted, ctx),
        plan(
            "trend_structure",
            Side.LONG,
            setup_id=exhausted.setup_id,
        ),
        activity_rank=5,
        activity_score=50,
    )

    assert fresh_priority.key() > exhausted_priority.key()


def test_activity_is_only_late_tiebreak_after_shared_semantic_dimensions() -> None:
    ctx = context()
    trade = decision(
        "trend_structure",
        Action.LONG,
        details={
            "entryFreshness": {"classification": "fresh"},
            "flowAlignment": {"classification": "aligned"},
            "liquidityAlignment": {"classification": "neutral"},
        },
    )
    assessment = assess_candidate(trade, ctx)
    shared_plan = plan(
        "trend_structure",
        Side.LONG,
        setup_id=trade.setup_id,
    )

    hot = build_selection_priority(
        assessment,
        shared_plan,
        activity_rank=3,
        activity_score=90,
    )
    quiet = build_selection_priority(
        assessment,
        shared_plan,
        activity_rank=8,
        activity_score=10,
    )

    assert hot.key() > quiet.key()
    assert hot.confluence_count == quiet.confluence_count
    assert hot.flow_priority == quiet.flow_priority
    assert hot.liquidity_priority == quiet.liquidity_priority
    assert hot.freshness_priority == quiet.freshness_priority
    assert hot.net_reward_risk == quiet.net_reward_risk



def test_breakout_overlap_with_day_high_is_not_own_level_exemption() -> None:
    day_high = mature_level(
        "day_high",
        100.05,
        100.05,
        generation="DAY:H",
    )
    ctx = context(resistance=day_high)
    trade = decision(
        "level_breakout",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
        watched_level=100.0,
        details={
            "state": "impulse",
            "zone": {
                "kind": "resistance",
                "low": 99.95,
                "high": 100.10,
            },
            "levelLifecycle": {
                "generation_id": "R:own:g1",
            },
            "opportunityFreshness": {
                "classification": "fresh",
            },
            "flowAlignment": {
                "classification": "strongly_aligned",
            },
            "liquidityAlignment": {
                "classification": "supportive",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert (
        assessment.structural_path.own_breakout_level_exempted
        is False
    )
    assert (
        assessment.structural_path.obstacle_before_first_take
        is True
    )
    assert assessment.structural_path.risk_scale == pytest.approx(
        0.65
    )



def test_fresh_single_touch_day_high_is_still_structural_obstacle() -> None:
    day_high = StructuralLevel(
        kind="day_high",
        low=100.20,
        high=100.20,
        touches=1,
        timeframe="1D",
        score=0.9,
        reaction_pct=0.0,
        volume_ratio=1.0,
        generation_id="DAY:H:fresh",
        distinct_approaches=0,
        dwell_bars=0,
        lifecycle="fresh",
    )
    ctx = context(resistance=day_high)
    trade = decision(
        "weak_level_rejection",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
    )

    path = assess_structural_path(trade, ctx)

    assert path.obstacle_before_first_take is True
    assert path.blocked is True
    assert path.obstacle["kind"] == "day_high"



def test_day_high_is_not_owned_even_if_generation_id_is_reused() -> None:
    shared_generation = "R:resistance:100:g1"
    day_high = mature_level(
        "day_high",
        100.05,
        100.05,
        generation=shared_generation,
    )
    ctx = context(resistance=day_high)
    trade = decision(
        "level_breakout",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
        watched_level=100.0,
        details={
            "state": "impulse",
            "zone": {
                "kind": "resistance",
                "low": 99.95,
                "high": 100.10,
            },
            "levelLifecycle": {
                "generation_id": shared_generation,
            },
            "opportunityFreshness": {
                "classification": "fresh",
            },
            "flowAlignment": {
                "classification": "strongly_aligned",
            },
            "liquidityAlignment": {
                "classification": "supportive",
            },
        },
    )

    assessment = assess_candidate(trade, ctx)

    assert (
        assessment.structural_path.own_breakout_level_exempted
        is False
    )
    assert assessment.structural_path.obstacle_before_first_take



def test_structural_path_uses_final_target_when_partial_is_not_economically_planned() -> None:
    resistance = mature_level(
        "resistance",
        100.65,
        100.75,
        generation="R:g-partial-gap",
    )
    ctx = context(resistance=resistance)
    trade = decision(
        "weak_level_rejection",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
    )

    with_partial = assess_candidate(
        trade,
        ctx,
        partial_take_at_r=1.0,
        partial_take_enabled=True,
    )
    assert with_partial.allowed is True
    assert (
        with_partial.structural_path.obstacle_before_first_take
        is False
    )

    trade.details["plannedPartialEnabled"] = False
    without_partial = assess_candidate(
        trade,
        ctx,
        partial_take_at_r=1.0,
        partial_take_enabled=True,
    )

    assert without_partial.allowed is False
    assert (
        without_partial.structural_path.obstacle_before_first_take
        is True
    )
    assert (
        "mature_structural_obstacle_before_first_take"
        in without_partial.blockers
    )



def test_arbiter_skips_weak_nearest_level_and_finds_next_mature_obstacle() -> None:
    weak_nearest = StructuralLevel(
        kind="resistance",
        low=100.10,
        high=100.15,
        touches=1,
        timeframe="1m",
        score=0.3,
        lifecycle="fresh",
        distinct_approaches=1,
    )
    mature_farther = mature_level(
        "day_high",
        100.35,
        100.35,
        generation="R:day_high:g1",
    )
    structure = StructureContext(
        reference_price=100.0,
        level_count=2,
        trendline_count=0,
        nearest_support=None,
        nearest_resistance=weak_nearest,
        support_distance_pct=None,
        resistance_distance_pct=0.001,
        support_trendline=None,
        resistance_trendline=None,
        support_levels=(),
        resistance_levels=(
            weak_nearest,
            mature_farther,
        ),
    )
    ctx = MarketContext(
        symbol="AAAUSDT",
        observed_at_ms=100_000,
        last_price=100.0,
        legacy_trend=Trend.FLAT,
        htf_bias=None,
        local_regime=None,
        flow=None,
        liquidity=None,
        structure=structure,
        execution=execution_ready(),
    )
    trade = decision(
        "weak_level_rejection",
        Action.LONG,
        entry=100.0,
        stop=99.50,
        target=101.0,
    )

    assessment = assess_candidate(
        trade,
        ctx,
        partial_take_at_r=1.0,
        partial_take_enabled=True,
    )

    assert assessment.allowed is False
    assert assessment.structural_path.obstacle["kind"] == "day_high"
    assert assessment.structural_path.obstacle_before_first_take is True
