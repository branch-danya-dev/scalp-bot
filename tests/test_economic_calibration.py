import pytest

from scalp_bot.economic_calibration import (
    build_conditional_economic_calibration,
)


def trade(
    *,
    strategy: str = "trend_structure",
    side: str = "long",
    regime: str = "bullish_trend",
    planned_rr: float,
    realized_all_in_r: float,
    net_pnl: float,
    winner_cost_share: float = 0.30,
    first_take_move_pct: float = 0.003,
    would_fail_min_profit: bool = False,
    would_fail_rr: bool | None = None,
    would_fail_first_take: bool = False,
) -> dict:
    if would_fail_rr is None:
        would_fail_rr = planned_rr < 1.15
    return {
        "strategy": strategy,
        "side": side,
        "regime": regime,
        "plannedNetRewardRisk": planned_rr,
        "realizedAllInR": realized_all_in_r,
        "realizedR": realized_all_in_r,
        "grossPnl": net_pnl + 1.0,
        "fees": 1.0,
        "netPnl": net_pnl,
        "plannedNetAtTargetUsd": planned_rr * 5.0,
        "plannedAllInLossUsd": 5.0,
        "winnerCostShare": winner_cost_share,
        "stopCostShare": 0.25,
        "firstTakeMovePct": first_take_move_pct,
        "wouldFailMinimumNetProfit": would_fail_min_profit,
        "wouldFailNetRewardRisk": would_fail_rr,
        "wouldFailFirstTakeMove": would_fail_first_take,
    }


def test_insufficient_group_does_not_emit_policy_candidate() -> None:
    rows = [
        trade(
            planned_rr=0.9,
            realized_all_in_r=-0.5,
            net_pnl=-2.5,
        )
        for _ in range(4)
    ]

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=10,
        minimum_segment_samples=3,
    )

    group = report["groups"][0]
    assert group["sampleReady"] is False
    assert group["policyStatus"] == "insufficient_samples"
    assert group["bestObservedRrThreshold"] is None
    assert report["summary"]["sampleReadyExactGroups"] == 0
    assert report["policy"]["enforcement"] == "disabled"


def test_legacy_115_can_improve_observed_expectancy_for_one_group() -> None:
    rows = []
    rows.extend(
        trade(
            planned_rr=0.9,
            realized_all_in_r=-0.8,
            net_pnl=-4.0,
        )
        for _ in range(10)
    )
    rows.extend(
        trade(
            planned_rr=1.3,
            realized_all_in_r=0.6,
            net_pnl=3.0,
        )
        for _ in range(10)
    )

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=12,
        minimum_segment_samples=8,
    )

    group = report["groups"][0]
    assert group["sampleReady"] is True
    legacy = group["legacyUniversal115"]
    assert (
        legacy["verdict"]
        == "improves_observed_expectancy"
    )
    counterfactual = legacy["counterfactual"]
    assert counterfactual["retainedSamples"] == 10
    assert counterfactual["expectancyAllInR"] == pytest.approx(0.6)
    assert counterfactual["deltaExpectancyAllInR"] > 0.5
    assert report["summary"]["legacy115ImprovesObservedExpectancy"] == 1


def test_legacy_115_can_reduce_observed_expectancy_for_another_group() -> None:
    rows = []
    rows.extend(
        trade(
            strategy="trend_structure",
            side="short",
            regime="bearish_trend",
            planned_rr=0.8,
            realized_all_in_r=0.5,
            net_pnl=2.5,
        )
        for _ in range(10)
    )
    rows.extend(
        trade(
            strategy="trend_structure",
            side="short",
            regime="bearish_trend",
            planned_rr=1.4,
            realized_all_in_r=-0.4,
            net_pnl=-2.0,
        )
        for _ in range(10)
    )

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=12,
        minimum_segment_samples=8,
    )

    group = report["groups"][0]
    assert (
        group["legacyUniversal115"]["verdict"]
        == "reduces_observed_expectancy"
    )
    assert report["summary"]["legacy115ReducesObservedExpectancy"] == 1


def test_best_observed_threshold_is_explicitly_in_sample_only() -> None:
    rows = []
    rows.extend(
        trade(
            planned_rr=0.6,
            realized_all_in_r=-0.3,
            net_pnl=-1.5,
        )
        for _ in range(8)
    )
    rows.extend(
        trade(
            planned_rr=1.0,
            realized_all_in_r=0.1,
            net_pnl=0.5,
        )
        for _ in range(8)
    )
    rows.extend(
        trade(
            planned_rr=1.5,
            realized_all_in_r=0.8,
            net_pnl=4.0,
        )
        for _ in range(8)
    )

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=20,
        minimum_segment_samples=6,
    )

    candidate = report["groups"][0]["bestObservedRrThreshold"]
    assert candidate is not None
    assert candidate["inSampleOnly"] is True
    assert candidate["policyEligible"] is False
    assert "same trades" in report["policy"]["warning"]


def test_recorded_gate_counterfactuals_keep_pass_and_fail_outcomes_separate() -> None:
    rows = [
        trade(
            planned_rr=0.8,
            realized_all_in_r=0.4,
            net_pnl=2.0,
            would_fail_min_profit=True,
        ),
        trade(
            planned_rr=1.4,
            realized_all_in_r=-0.5,
            net_pnl=-2.5,
            would_fail_min_profit=False,
        ),
    ]

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=2,
        minimum_segment_samples=1,
    )

    counter = report["groups"][0][
        "recordedGateCounterfactuals"
    ]["minimumNetProfit"]
    assert counter["knownSamples"] == 2
    assert counter["pass"]["samples"] == 1
    assert counter["fail"]["samples"] == 1
    assert counter["pass"]["netPnl"] == pytest.approx(-2.5)
    assert counter["fail"]["netPnl"] == pytest.approx(2.0)


def test_groups_are_conditioned_by_strategy_side_and_regime() -> None:
    rows = [
        trade(
            strategy="trend_structure",
            side="long",
            regime="bullish_trend",
            planned_rr=1.0,
            realized_all_in_r=-0.5,
            net_pnl=-2.5,
        ),
        trade(
            strategy="trend_structure",
            side="short",
            regime="bearish_trend",
            planned_rr=1.0,
            realized_all_in_r=0.5,
            net_pnl=2.5,
        ),
        trade(
            strategy="level_breakout",
            side="long",
            regime="range",
            planned_rr=1.0,
            realized_all_in_r=0.2,
            net_pnl=1.0,
        ),
    ]

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=1,
        minimum_segment_samples=1,
    )

    keys = {
        (
            row["strategy"],
            row["side"],
            row["regime"],
        )
        for row in report["groups"]
    }
    assert keys == {
        ("trend_structure", "long", "bullish_trend"),
        ("trend_structure", "short", "bearish_trend"),
        ("level_breakout", "long", "range"),
    }
    # Broader scopes exist only as references, never as the exact policy key.
    assert any(
        row["scope"] == "strategy_side"
        for row in report["referenceGroups"]
    )
    assert any(
        row["scope"] == "strategy"
        for row in report["referenceGroups"]
    )


def test_cost_share_bands_are_reported_without_becoming_a_gate() -> None:
    rows = [
        trade(
            planned_rr=1.0,
            realized_all_in_r=0.4,
            net_pnl=2.0,
            winner_cost_share=0.18,
        ),
        trade(
            planned_rr=1.0,
            realized_all_in_r=-0.4,
            net_pnl=-2.0,
            winner_cost_share=0.45,
        ),
    ]

    report = build_conditional_economic_calibration(
        rows,
        minimum_group_samples=2,
        minimum_segment_samples=1,
    )

    group = report["groups"][0]
    bands = {
        row["band"]: row
        for row in group["winnerCostShareBands"]
    }
    assert bands["<20%"]["samples"] == 1
    assert bands["35-50%"]["samples"] == 1
    assert report["policy"]["enforcement"] == "disabled"
