from scalp_bot.domain import Action, Trend
from scalp_bot.strategy.flow_context import (
    FlowAlignmentClass,
    build_multi_horizon_flow_context,
)


def trade_flow(
    i5: float,
    i15: float,
    i60: float,
    *,
    count: int = 20,
) -> dict:
    return {
        "imbalance5s": i5,
        "imbalance15s": i15,
        "imbalance60s": i60,
        "cvd5s": i5 * 10_000,
        "cvd15s": i15 * 20_000,
        "cvd60s": i60 * 50_000,
        "notional5s": 10_000.0,
        "notional15s": 20_000.0,
        "notional60s": 50_000.0,
        "tradeCount5s": count,
        "tradeCount15s": count * 2,
        "tradeCount60s": count * 4,
    }


def book_flow(
    n5: float,
    n15: float,
    n60: float,
    *,
    events: int = 10,
) -> dict:
    depth = 100_000.0
    return {
        "bestLevelOfiUsd5s": n5 * depth,
        "bestLevelOfiUsd15s": n15 * depth,
        "bestLevelOfiUsd60s": n60 * depth,
        "normalizedOfi5s": n5,
        "normalizedOfi15s": n15,
        "normalizedOfi60s": n60,
        "eventCount5s": events,
        "eventCount15s": events * 2,
        "eventCount60s": events * 4,
        "top5DepthUsd": depth,
    }


def test_strongly_aligned_long_requires_all_three_horizons() -> None:
    context = build_multi_horizon_flow_context(
        trade_flow(0.8, 0.6, 0.4),
        book_flow(0.10, 0.08, 0.06),
        observed_at_ms=100_000,
    )

    assert context.dominant_direction == Trend.UP
    assert (
        context.long_alignment.classification
        == FlowAlignmentClass.STRONGLY_ALIGNED
    )
    assert context.long_alignment.aligned_horizons == [5, 15, 60]
    assert (
        context.short_alignment.classification
        == FlowAlignmentClass.OPPOSED
    )


def test_short_term_reversal_detects_5s_bounce_against_longer_flow() -> None:
    context = build_multi_horizon_flow_context(
        trade_flow(0.85, -0.45, -0.60),
        book_flow(0.08, -0.07, -0.09),
        observed_at_ms=100_000,
    )

    assert (
        context.long_alignment.classification
        == FlowAlignmentClass.SHORT_TERM_REVERSAL
    )
    assert 5 in context.long_alignment.aligned_horizons
    assert 15 in context.long_alignment.opposed_horizons
    assert 60 in context.long_alignment.opposed_horizons
    assert context.dominant_direction == Trend.DOWN


def test_long_is_opposed_when_multiple_horizons_are_bearish() -> None:
    context = build_multi_horizon_flow_context(
        trade_flow(-0.7, -0.5, -0.4),
        book_flow(-0.08, -0.07, -0.05),
        observed_at_ms=100_000,
    )

    alignment = context.alignment_for(Action.LONG)
    assert alignment is not None
    assert alignment.classification == FlowAlignmentClass.OPPOSED
    assert len(alignment.opposed_horizons) == 3


def test_missing_flow_is_reported_as_insufficient_not_neutral_confirmation() -> None:
    context = build_multi_horizon_flow_context(
        trade_flow(0.0, 0.0, 0.0, count=0),
        book_flow(0.0, 0.0, 0.0, events=0),
        observed_at_ms=100_000,
    )

    assert context.dominant_direction == Trend.FLAT
    assert context.directional_score is None
    assert (
        context.long_alignment.classification
        == FlowAlignmentClass.INSUFFICIENT_DATA
    )
    assert (
        context.short_alignment.classification
        == FlowAlignmentClass.INSUFFICIENT_DATA
    )


def test_public_contract_contains_raw_horizon_evidence_and_side_alignment() -> None:
    context = build_multi_horizon_flow_context(
        trade_flow(0.5, 0.3, 0.2),
        book_flow(0.04, 0.03, 0.02),
        observed_at_ms=123_000,
    )

    public = context.public()

    assert public["observedAtMs"] == 123_000
    assert public["horizons"]["5s"]["tradeImbalance"] == 0.5
    assert public["horizons"]["15s"]["cvdUsd"] == 6_000
    assert "longAlignment" in public
    assert "shortAlignment" in public
