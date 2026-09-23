from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Callable


DEFAULT_RR_THRESHOLDS = (
    0.50,
    0.75,
    1.00,
    1.15,
    1.25,
    1.50,
    2.00,
)

DEFAULT_WINNER_COST_SHARE_THRESHOLDS = (
    0.20,
    0.35,
    0.50,
    0.75,
)

DEFAULT_RR_BANDS = (
    (None, 0.50),
    (0.50, 0.75),
    (0.75, 1.00),
    (1.00, 1.15),
    (1.15, 1.25),
    (1.25, 1.50),
    (1.50, 2.00),
    (2.00, None),
)

DEFAULT_COST_SHARE_BANDS = (
    (None, 0.20),
    (0.20, 0.35),
    (0.35, 0.50),
    (0.50, 0.75),
    (0.75, None),
)


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _band_label(
    low: float | None,
    high: float | None,
) -> str:
    if low is None and high is not None:
        return f"<{high:.2f}"
    if low is not None and high is None:
        return f">={low:.2f}"
    if low is None or high is None:
        return "unknown"
    return f"{low:.2f}-{high:.2f}"


def _pct_band_label(
    low: float | None,
    high: float | None,
) -> str:
    if low is None and high is not None:
        return f"<{high * 100:.0f}%"
    if low is not None and high is None:
        return f">={low * 100:.0f}%"
    if low is None or high is None:
        return "unknown"
    return f"{low * 100:.0f}-{high * 100:.0f}%"


def _in_band(
    value: float,
    low: float | None,
    high: float | None,
) -> bool:
    if low is not None and value < low:
        return False
    if high is not None and value >= high:
        return False
    return True


def _planned_breakeven_win_rate(
    rr: float,
) -> float | None:
    if rr < 0:
        return None
    return 1.0 / (1.0 + rr)


def _outcome_summary(
    rows: list[dict],
) -> dict[str, Any]:
    nets = [float(row.get("netPnl") or 0.0) for row in rows]
    gross = [
        float(row.get("grossPnl") or 0.0)
        for row in rows
    ]
    fees = [float(row.get("fees") or 0.0) for row in rows]
    all_in_r = [
        float(value)
        for row in rows
        if (
            value := row.get("realizedAllInR")
        ) is not None
    ]
    structural_r = [
        float(value)
        for row in rows
        if (
            value := row.get("realizedR")
        ) is not None
    ]
    planned_rr = [
        float(value)
        for row in rows
        if (
            value := row.get("plannedNetRewardRisk")
        ) is not None
    ]
    planned_net = [
        float(value)
        for row in rows
        if (
            value := row.get("plannedNetAtTargetUsd")
        ) is not None
    ]
    planned_loss = [
        float(value)
        for row in rows
        if (
            value := row.get("plannedAllInLossUsd")
        ) is not None
    ]
    winner_cost_share = [
        float(value)
        for row in rows
        if (
            value := row.get("winnerCostShare")
        ) is not None
    ]
    stop_cost_share = [
        float(value)
        for row in rows
        if (
            value := row.get("stopCostShare")
        ) is not None
    ]
    first_take = [
        float(value)
        for row in rows
        if (
            value := row.get("firstTakeMovePct")
        ) is not None
    ]

    wins = sum(value > 0 for value in nets)
    losses = sum(value < 0 for value in nets)
    breakeven = len(rows) - wins - losses
    win_rate = (
        wins / len(rows)
        if rows
        else None
    )

    breakeven_rates = [
        rate
        for rr in planned_rr
        if (
            rate := _planned_breakeven_win_rate(rr)
        ) is not None
    ]
    average_breakeven = _mean(breakeven_rates)

    return {
        "samples": len(rows),
        "allInRSamples": len(all_in_r),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "winRate": win_rate,
        "grossPnl": sum(gross),
        "fees": sum(fees),
        "netPnl": sum(nets),
        "netPerTrade": _mean(nets),
        "expectancyAllInR": _mean(all_in_r),
        "medianAllInR": _median(all_in_r),
        "expectancyStructuralR": _mean(structural_r),
        "averagePlannedNetRewardRisk": _mean(planned_rr),
        "medianPlannedNetRewardRisk": _median(planned_rr),
        "averagePlannedNetAtTargetUsd": _mean(planned_net),
        "averagePlannedAllInLossUsd": _mean(planned_loss),
        "averageWinnerCostShare": _mean(winner_cost_share),
        "medianWinnerCostShare": _median(winner_cost_share),
        "averageStopCostShare": _mean(stop_cost_share),
        "averageFirstTakeMovePct": _mean(first_take),
        "averagePlannedBreakevenWinRate": average_breakeven,
        "winRateMinusPlannedBreakeven": (
            win_rate - average_breakeven
            if (
                win_rate is not None
                and average_breakeven is not None
            )
            else None
        ),
    }


def _threshold_row(
    rows: list[dict],
    *,
    threshold: float,
    predicate: Callable[[dict, float], bool],
    baseline: dict[str, Any],
    minimum_segment_samples: int,
) -> dict[str, Any]:
    retained = [
        row
        for row in rows
        if predicate(row, threshold)
    ]
    summary = _outcome_summary(retained)
    expectancy = summary.get("expectancyAllInR")
    baseline_expectancy = baseline.get(
        "expectancyAllInR"
    )
    return {
        "threshold": threshold,
        "retainedSamples": len(retained),
        "coverageRate": (
            len(retained) / len(rows)
            if rows
            else None
        ),
        "sampleReady": (
            summary["allInRSamples"]
            >= minimum_segment_samples
        ),
        **summary,
        "deltaExpectancyAllInR": (
            expectancy - baseline_expectancy
            if (
                expectancy is not None
                and baseline_expectancy is not None
            )
            else None
        ),
        "deltaNetPnl": (
            summary["netPnl"]
            - float(baseline.get("netPnl") or 0.0)
        ),
    }


def _split_counterfactual(
    rows: list[dict],
    *,
    key: str,
    fail_when_true: bool = True,
) -> dict[str, Any]:
    known = [
        row
        for row in rows
        if row.get(key) is not None
    ]
    if fail_when_true:
        passed = [
            row
            for row in known
            if row.get(key) is False
        ]
        failed = [
            row
            for row in known
            if row.get(key) is True
        ]
    else:
        passed = [
            row
            for row in known
            if row.get(key) is True
        ]
        failed = [
            row
            for row in known
            if row.get(key) is False
        ]
    return {
        "knownSamples": len(known),
        "pass": _outcome_summary(passed),
        "fail": _outcome_summary(failed),
    }


def _band_rows(
    rows: list[dict],
    *,
    key: str,
    bands: tuple[tuple[float | None, float | None], ...],
    label_fn: Callable[
        [float | None, float | None],
        str,
    ],
) -> list[dict]:
    result = []
    for low, high in bands:
        selected = []
        for row in rows:
            value = _safe_float(row.get(key))
            if value is None:
                continue
            if _in_band(value, low, high):
                selected.append(row)
        if not selected:
            continue
        result.append({
            "band": label_fn(low, high),
            "low": low,
            "high": high,
            **_outcome_summary(selected),
        })
    return result


def _best_observed_threshold(
    rows: list[dict],
) -> dict[str, Any] | None:
    ready = [
        row
        for row in rows
        if row.get("sampleReady")
        and row.get("expectancyAllInR") is not None
    ]
    if not ready:
        return None
    best = max(
        ready,
        key=lambda row: (
            float(row.get("expectancyAllInR") or 0.0),
            float(row.get("coverageRate") or 0.0),
            -float(row.get("threshold") or 0.0),
        ),
    )
    return {
        "threshold": best["threshold"],
        "expectancyAllInR": best["expectancyAllInR"],
        "coverageRate": best["coverageRate"],
        "retainedSamples": best["retainedSamples"],
        "deltaExpectancyAllInR": (
            best["deltaExpectancyAllInR"]
        ),
        "inSampleOnly": True,
        "policyEligible": False,
    }


def _calibrate_group(
    rows: list[dict],
    *,
    strategy: str,
    side: str,
    regime: str,
    scope: str,
    minimum_group_samples: int,
    minimum_segment_samples: int,
    rr_thresholds: tuple[float, ...],
    cost_share_thresholds: tuple[float, ...],
) -> dict[str, Any]:
    baseline = _outcome_summary(rows)
    sample_ready = (
        baseline["allInRSamples"]
        >= minimum_group_samples
    )

    rr_sweep = [
        _threshold_row(
            rows,
            threshold=threshold,
            predicate=lambda row, value: (
                (
                    planned := _safe_float(
                        row.get(
                            "plannedNetRewardRisk"
                        )
                    )
                )
                is not None
                and planned >= value
            ),
            baseline=baseline,
            minimum_segment_samples=(
                minimum_segment_samples
            ),
        )
        for threshold in rr_thresholds
    ]

    cost_sweep = [
        _threshold_row(
            rows,
            threshold=threshold,
            predicate=lambda row, value: (
                (
                    cost := _safe_float(
                        row.get("winnerCostShare")
                    )
                )
                is not None
                and cost <= value
            ),
            baseline=baseline,
            minimum_segment_samples=(
                minimum_segment_samples
            ),
        )
        for threshold in cost_share_thresholds
    ]

    legacy_115 = next(
        (
            row
            for row in rr_sweep
            if abs(
                float(row["threshold"]) - 1.15
            ) < 1e-9
        ),
        None,
    )
    legacy_verdict = "insufficient"
    if (
        legacy_115 is not None
        and sample_ready
        and legacy_115.get("sampleReady")
        and baseline.get("expectancyAllInR")
        is not None
        and legacy_115.get("expectancyAllInR")
        is not None
    ):
        delta = float(
            legacy_115[
                "deltaExpectancyAllInR"
            ]
            or 0.0
        )
        if delta > 0.05:
            legacy_verdict = "improves_observed_expectancy"
        elif delta < -0.05:
            legacy_verdict = "reduces_observed_expectancy"
        else:
            legacy_verdict = "roughly_neutral"

    return {
        "scope": scope,
        "strategy": strategy,
        "side": side,
        "regime": regime,
        "sampleReady": sample_ready,
        "baseline": baseline,
        "netRewardRiskBands": _band_rows(
            rows,
            key="plannedNetRewardRisk",
            bands=DEFAULT_RR_BANDS,
            label_fn=_band_label,
        ),
        "winnerCostShareBands": _band_rows(
            rows,
            key="winnerCostShare",
            bands=DEFAULT_COST_SHARE_BANDS,
            label_fn=_pct_band_label,
        ),
        "netRewardRiskThresholdSweep": rr_sweep,
        "winnerCostShareThresholdSweep": cost_sweep,
        "legacyUniversal115": {
            "verdict": legacy_verdict,
            "counterfactual": legacy_115,
        },
        "recordedGateCounterfactuals": {
            "minimumNetProfit": _split_counterfactual(
                rows,
                key="wouldFailMinimumNetProfit",
            ),
            "netRewardRisk": _split_counterfactual(
                rows,
                key="wouldFailNetRewardRisk",
            ),
            "firstTakeMove": _split_counterfactual(
                rows,
                key="wouldFailFirstTakeMove",
            ),
        },
        "bestObservedRrThreshold": (
            _best_observed_threshold(rr_sweep)
            if sample_ready
            else None
        ),
        "policyStatus": (
            "research_ready"
            if sample_ready
            else "insufficient_samples"
        ),
    }


def _group_rows(
    trades: list[dict],
    key_fn: Callable[[dict], tuple[str, str, str]],
) -> dict[tuple[str, str, str], list[dict]]:
    grouped: dict[
        tuple[str, str, str],
        list[dict],
    ] = defaultdict(list)
    for row in trades:
        grouped[key_fn(row)].append(row)
    return grouped


def build_conditional_economic_calibration(
    trades: list[dict],
    *,
    minimum_group_samples: int = 20,
    minimum_segment_samples: int = 8,
    rr_thresholds: tuple[float, ...] = (
        DEFAULT_RR_THRESHOLDS
    ),
    cost_share_thresholds: tuple[float, ...] = (
        DEFAULT_WINNER_COST_SHARE_THRESHOLDS
    ),
) -> dict[str, Any]:
    economic_trades = [
        row
        for row in trades
        if row.get("plannedNetRewardRisk") is not None
        and row.get("realizedAllInR") is not None
    ]

    exact_groups = _group_rows(
        economic_trades,
        lambda row: (
            str(row.get("strategy") or "unknown"),
            str(row.get("side") or "unknown"),
            str(row.get("regime") or "unknown"),
        ),
    )
    side_groups = _group_rows(
        economic_trades,
        lambda row: (
            str(row.get("strategy") or "unknown"),
            str(row.get("side") or "unknown"),
            "all",
        ),
    )
    strategy_groups = _group_rows(
        economic_trades,
        lambda row: (
            str(row.get("strategy") or "unknown"),
            "all",
            "all",
        ),
    )

    exact = [
        _calibrate_group(
            values,
            strategy=key[0],
            side=key[1],
            regime=key[2],
            scope="strategy_side_regime",
            minimum_group_samples=(
                minimum_group_samples
            ),
            minimum_segment_samples=(
                minimum_segment_samples
            ),
            rr_thresholds=rr_thresholds,
            cost_share_thresholds=(
                cost_share_thresholds
            ),
        )
        for key, values in sorted(exact_groups.items())
    ]
    references = [
        _calibrate_group(
            values,
            strategy=key[0],
            side=key[1],
            regime=key[2],
            scope=(
                "strategy_side"
                if key[1] != "all"
                else "strategy"
            ),
            minimum_group_samples=(
                minimum_group_samples
            ),
            minimum_segment_samples=(
                minimum_segment_samples
            ),
            rr_thresholds=rr_thresholds,
            cost_share_thresholds=(
                cost_share_thresholds
            ),
        )
        for grouped in (side_groups, strategy_groups)
        for key, values in sorted(grouped.items())
    ]

    ready = [
        row for row in exact if row["sampleReady"]
    ]
    legacy_improves = sum(
        row["legacyUniversal115"]["verdict"]
        == "improves_observed_expectancy"
        for row in ready
    )
    legacy_hurts = sum(
        row["legacyUniversal115"]["verdict"]
        == "reduces_observed_expectancy"
        for row in ready
    )

    return {
        "schemaVersion": 1,
        "policy": {
            "minimumGroupSamples": (
                minimum_group_samples
            ),
            "minimumSegmentSamples": (
                minimum_segment_samples
            ),
            "netRewardRiskThresholds": list(
                rr_thresholds
            ),
            "winnerCostShareThresholds": list(
                cost_share_thresholds
            ),
            "enforcement": "disabled",
            "calibrationMode": "in_sample_observation",
            "warning": (
                "Observed threshold sweeps use the same trades "
                "for measurement and threshold comparison. "
                "bestObservedRrThreshold is descriptive only "
                "and must be validated on later sessions before "
                "it can become a trading rule."
            ),
        },
        "summary": {
            "economicTrades": len(economic_trades),
            "exactGroups": len(exact),
            "sampleReadyExactGroups": len(ready),
            "legacy115ImprovesObservedExpectancy": (
                legacy_improves
            ),
            "legacy115ReducesObservedExpectancy": (
                legacy_hurts
            ),
        },
        "groups": exact,
        "referenceGroups": references,
    }
