import pytest

from scalp_bot.stability_validation import (
    build_stability_validation,
    validate_feature_stability,
    validate_fixed_rr_thresholds,
    validate_hindsight_coverage_stability,
    validate_interaction_stability,
    validate_threshold_selection_holdout,
)


def trade(
    session_id: str,
    *,
    flow: str,
    realized_r: float,
    rr: float = 1.2,
    freshness: str = "fresh",
    liquidity: str = "neutral",
    confluence: int = 0,
) -> dict:
    return {
        "sessionId": session_id,
        "strategy": "trend_structure",
        "side": "long",
        "regime": "bullish_trend",
        "flowAlignmentClass": flow,
        "freshnessClass": freshness,
        "liquidityAlignmentClass": liquidity,
        "confluenceCount": confluence,
        "plannedNetRewardRisk": rr,
        "realizedAllInR": realized_r,
        "realizedR": realized_r,
        "netPnl": realized_r * 5.0,
        "grossPnl": realized_r * 5.0 + 1.0,
        "fees": 1.0,
        "mfeR": max(0.0, realized_r + 0.6),
        "maeR": 0.2 if realized_r >= 0 else 0.8,
    }


def stable_feature_trades() -> list[dict]:
    rows = []
    for index in range(4):
        session = f"s{index + 1}"
        rows.extend(
            trade(
                session,
                flow="short_term_reversal",
                realized_r=-0.5,
            )
            for _ in range(2)
        )
        rows.extend(
            trade(
                session,
                flow="strongly_aligned",
                realized_r=0.4,
            )
            for _ in range(2)
        )
    return rows


def stable_threshold_trades() -> list[dict]:
    rows = []
    for index in range(4):
        session = f"s{index + 1}"
        rows.extend(
            trade(
                session,
                flow="aligned",
                rr=0.9,
                realized_r=-0.5,
            )
            for _ in range(2)
        )
        rows.extend(
            trade(
                session,
                flow="aligned",
                rr=1.3,
                realized_r=0.5,
            )
            for _ in range(2)
        )
    return rows


def test_feature_effect_is_stable_negative_across_session_holdouts() -> None:
    rows = validate_feature_stability(
        stable_feature_trades(),
        minimum_sessions=4,
        minimum_session_samples=2,
        minimum_train_samples=6,
    )

    effect = next(
        row
        for row in rows
        if (
            row["dimension"] == "flowAlignment"
            and row["value"] == "short_term_reversal"
        )
    )

    assert effect["pooledDeltaAllInR"] == pytest.approx(-0.9)
    assert effect["comparisonSessions"] == 4
    assert effect["leaveOneSessionOutFolds"] == 4
    assert effect["leaveOneSessionOutSignAgreementRate"] == 1.0
    assert effect["status"] == "stable_negative"
    assert effect["validationCandidate"] is True
    assert effect["livePolicyEligible"] is False


def test_feature_effect_is_not_promoted_when_one_session_dominates_samples() -> None:
    rows = []
    for index in range(4):
        session = f"s{index + 1}"
        selected_count = 10 if index == 0 else 2
        rows.extend(
            trade(
                session,
                flow="short_term_reversal",
                realized_r=-0.5,
            )
            for _ in range(selected_count)
        )
        rows.extend(
            trade(
                session,
                flow="strongly_aligned",
                realized_r=0.4,
            )
            for _ in range(2)
        )

    effects = validate_feature_stability(
        rows,
        minimum_sessions=4,
        minimum_session_samples=2,
        minimum_train_samples=6,
    )
    effect = next(
        row
        for row in effects
        if (
            row["dimension"] == "flowAlignment"
            and row["value"] == "short_term_reversal"
        )
    )

    assert effect["maxSessionSampleShare"] > 0.60
    assert effect["status"] == "session_concentrated"
    assert effect["validationCandidate"] is False


def test_fixed_115_threshold_is_stable_positive_when_pass_beats_fail_every_session() -> None:
    rows = validate_fixed_rr_thresholds(
        stable_threshold_trades(),
        minimum_sessions=4,
        minimum_session_samples=2,
        minimum_train_samples=6,
    )
    threshold = next(
        row
        for row in rows
        if abs(row["threshold"] - 1.15) < 1e-9
    )

    assert threshold["passMinusFailAllInR"] == pytest.approx(1.0)
    assert threshold["comparisonSessions"] == 4
    assert threshold["leaveOneSessionOutSignAgreementRate"] == 1.0
    assert threshold["status"] == "stable_positive"
    assert threshold["validationCandidate"] is True
    assert threshold["livePolicyEligible"] is False


def test_threshold_selection_is_evaluated_out_of_sample_per_session() -> None:
    rows = validate_threshold_selection_holdout(
        stable_threshold_trades(),
        minimum_sessions=4,
        minimum_session_samples=2,
        minimum_train_samples=6,
    )
    group = rows[0]

    assert group["folds"] == 4
    assert group["holdoutPositiveRate"] == 1.0
    assert group["medianHoldoutPassMinusFailAllInR"] == pytest.approx(1.0)
    assert group["status"] == "stable_positive_holdout"
    assert group["validationCandidate"] is True
    assert group["livePolicyEligible"] is False
    assert sum(group["selectedThresholdCounts"].values()) == 4


def interaction(session_id: str, classification: str) -> dict:
    return {
        "sessionId": session_id,
        "strategy": "trend_structure",
        "state": "continuation",
        "hypothesisSide": "long",
        "forward": {
            "120s": {
                "movementBands": {
                    "0.20%": {
                        "classification": classification,
                    }
                }
            }
        },
    }


def test_market_interaction_hypothesis_must_repeat_across_sessions() -> None:
    rows = []
    for index in range(4):
        session = f"s{index + 1}"
        rows.extend(
            interaction(session, "hypothesis_first")
            for _ in range(3)
        )
        rows.append(
            interaction(session, "opposite_first")
        )

    result = validate_interaction_stability(
        rows,
        minimum_sessions=4,
        minimum_resolved_per_session=2,
    )
    row = result[0]

    assert row["comparisonSessions"] == 4
    assert row["pooledHypothesisSuccessRate"] == pytest.approx(0.75)
    assert row["medianSessionHypothesisSuccessRate"] == pytest.approx(0.75)
    assert row["status"] == "stable_hypothesis_first"


def hindsight_opportunity(
    session_id: str,
    *,
    fit: str,
    traded: bool,
) -> dict:
    return {
        "sessionId": session_id,
        "side": "long",
        "entryLocalRegime": "bullish_trend",
        "strategyFit": {
            "strategies": [
                {
                    "strategy": "trend_structure",
                    "fit": fit,
                },
                {
                    "strategy": "orderbook_density",
                    "fit": "tradeable_aligned",
                },
            ]
        },
        "botComparison": {
            "classification": (
                "traded" if traded else "missed"
            ),
            "trade": (
                {"strategy": "trend_structure"}
                if traded
                else None
            ),
        },
    }


def test_hindsight_coverage_stability_reports_session_dispersion() -> None:
    rows = []
    for index in range(4):
        session = f"s{index + 1}"
        rows.append(
            hindsight_opportunity(
                session,
                fit="tradeable_aligned",
                traded=True,
            )
        )
        rows.append(
            hindsight_opportunity(
                session,
                fit="observed_aligned",
                traded=False,
            )
        )

    result = validate_hindsight_coverage_stability(
        rows,
        minimum_sessions=4,
        minimum_opportunities_per_session=2,
    )
    row = next(
        value
        for value in result
        if value["strategy"] == "trend_structure"
    )

    assert row["sampleReady"] is True
    assert row["comparisonSessions"] == 4
    assert row["medianObservedCoverageRate"] == 1.0
    assert row["medianTradeableCoverageRate"] == pytest.approx(0.5)
    assert row["medianTradeCoverageRate"] == pytest.approx(0.5)
    assert all(
        value["strategy"] != "orderbook_density"
        for value in result
    )


def test_build_stability_validation_never_marks_live_policy_eligible() -> None:
    report = build_stability_validation(
        trades=stable_threshold_trades(),
        hindsight=[],
        interactions=[],
        minimum_sessions=4,
        minimum_session_samples=2,
        minimum_train_samples=6,
    )

    assert report["policy"]["livePolicyEnforcement"] == "disabled"
    assert report["summary"]["stablePositiveFixedRrThresholds"] > 0
    assert report["summary"]["stableThresholdSelectionGroups"] == 1
    assert all(
        row["livePolicyEligible"] is False
        for row in report["fixedNetRewardRiskThresholds"]
    )
