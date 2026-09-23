from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any, Callable, Iterable

from .economic_calibration import DEFAULT_RR_THRESHOLDS


TRADEABLE_PLAYBOOKS = {
    "trend_structure",
    "weak_level_rejection",
    "level_breakout",
}

FEATURE_DIMENSIONS = {
    "flowAlignment": "flowAlignmentClass",
    "entryFreshness": "freshnessClass",
    "liquidityAlignment": "liquidityAlignmentClass",
    "confluenceCount": "confluenceCount",
}


def _safe_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _expectancy(rows: Iterable[dict]) -> tuple[float | None, int]:
    values = [
        float(value)
        for row in rows
        if (value := row.get("realizedAllInR")) is not None
    ]
    return (_mean(values), len(values))


def _sign(value: float | None, epsilon: float) -> int:
    if value is None:
        return 0
    if value > epsilon:
        return 1
    if value < -epsilon:
        return -1
    return 0


def _session_counts(rows: list[dict]) -> Counter[str]:
    return Counter(str(row.get("sessionId") or "") for row in rows)


def _max_session_share(rows: list[dict]) -> float | None:
    if not rows:
        return None
    counts = _session_counts(rows)
    return max(counts.values()) / len(rows)


def _effect_status(
    *,
    pooled_delta: float | None,
    session_deltas: list[float],
    loo_agreement_rate: float | None,
    comparison_sessions: int,
    loo_folds: int,
    minimum_sessions: int,
    minimum_effect_r: float,
    sign_agreement_rate: float,
    max_session_share: float | None,
) -> str:
    if (
        pooled_delta is None
        or comparison_sessions < minimum_sessions
        or loo_folds < minimum_sessions
    ):
        return "insufficient_sessions"
    if abs(pooled_delta) < minimum_effect_r:
        return "weak_effect"
    if max_session_share is not None and max_session_share > 0.60:
        return "session_concentrated"
    if (
        loo_agreement_rate is not None
        and loo_agreement_rate >= sign_agreement_rate
    ):
        return (
            "stable_positive"
            if pooled_delta > 0
            else "stable_negative"
        )
    return "unstable"


def _value_for_dimension(
    row: dict,
    dimension: str,
    key: str,
) -> str:
    raw = row.get(key)
    if dimension == "confluenceCount" and isinstance(raw, (int, float)):
        return str(int(raw))
    return str(raw or "unknown")


def validate_feature_stability(
    trades: list[dict],
    *,
    minimum_sessions: int = 4,
    minimum_session_samples: int = 2,
    minimum_train_samples: int = 6,
    neutral_epsilon_r: float = 0.05,
    minimum_effect_r: float = 0.10,
    sign_agreement_rate: float = 0.75,
) -> list[dict]:
    exact_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in trades:
        if row.get("realizedAllInR") is None:
            continue
        exact_groups[
            (
                str(row.get("strategy") or "unknown"),
                str(row.get("side") or "unknown"),
                str(row.get("regime") or "unknown"),
            )
        ].append(row)

    result: list[dict] = []
    for (strategy, side, regime), group_rows in sorted(exact_groups.items()):
        for dimension, key in FEATURE_DIMENSIONS.items():
            values = sorted({
                _value_for_dimension(row, dimension, key)
                for row in group_rows
            })
            for value in values:
                selected = [
                    row
                    for row in group_rows
                    if _value_for_dimension(row, dimension, key) == value
                ]
                comparator = [
                    row
                    for row in group_rows
                    if _value_for_dimension(row, dimension, key) != value
                ]
                selected_expectancy, selected_samples = _expectancy(selected)
                comparator_expectancy, comparator_samples = _expectancy(comparator)
                pooled_delta = (
                    selected_expectancy - comparator_expectancy
                    if (
                        selected_expectancy is not None
                        and comparator_expectancy is not None
                    )
                    else None
                )

                session_ids = sorted({
                    str(row.get("sessionId") or "")
                    for row in group_rows
                })
                session_effects: list[dict] = []
                session_deltas: list[float] = []
                for session_id in session_ids:
                    session_selected = [
                        row
                        for row in selected
                        if str(row.get("sessionId") or "") == session_id
                    ]
                    session_comparator = [
                        row
                        for row in comparator
                        if str(row.get("sessionId") or "") == session_id
                    ]
                    selected_e, selected_n = _expectancy(session_selected)
                    comparator_e, comparator_n = _expectancy(session_comparator)
                    if (
                        selected_n < minimum_session_samples
                        or comparator_n < minimum_session_samples
                        or selected_e is None
                        or comparator_e is None
                    ):
                        continue
                    delta = selected_e - comparator_e
                    session_deltas.append(delta)
                    session_effects.append({
                        "sessionId": session_id,
                        "selectedSamples": selected_n,
                        "comparatorSamples": comparator_n,
                        "selectedExpectancyAllInR": selected_e,
                        "comparatorExpectancyAllInR": comparator_e,
                        "deltaAllInR": delta,
                        "sign": _sign(delta, neutral_epsilon_r),
                    })

                pooled_sign = _sign(pooled_delta, neutral_epsilon_r)
                non_neutral_session_signs = [
                    _sign(delta, neutral_epsilon_r)
                    for delta in session_deltas
                    if _sign(delta, neutral_epsilon_r) != 0
                ]
                same_sign_sessions = sum(
                    sign == pooled_sign
                    for sign in non_neutral_session_signs
                    if pooled_sign != 0
                )
                per_session_sign_rate = (
                    same_sign_sessions / len(non_neutral_session_signs)
                    if non_neutral_session_signs and pooled_sign != 0
                    else None
                )

                loo_folds: list[dict] = []
                agreements = 0
                comparable_folds = 0
                for holdout_session in session_ids:
                    train_selected = [
                        row for row in selected
                        if str(row.get("sessionId") or "") != holdout_session
                    ]
                    train_comparator = [
                        row for row in comparator
                        if str(row.get("sessionId") or "") != holdout_session
                    ]
                    test_selected = [
                        row for row in selected
                        if str(row.get("sessionId") or "") == holdout_session
                    ]
                    test_comparator = [
                        row for row in comparator
                        if str(row.get("sessionId") or "") == holdout_session
                    ]
                    train_selected_e, train_selected_n = _expectancy(train_selected)
                    train_comparator_e, train_comparator_n = _expectancy(train_comparator)
                    test_selected_e, test_selected_n = _expectancy(test_selected)
                    test_comparator_e, test_comparator_n = _expectancy(test_comparator)
                    if (
                        train_selected_n < minimum_train_samples
                        or train_comparator_n < minimum_train_samples
                        or test_selected_n < minimum_session_samples
                        or test_comparator_n < minimum_session_samples
                        or train_selected_e is None
                        or train_comparator_e is None
                        or test_selected_e is None
                        or test_comparator_e is None
                    ):
                        continue
                    train_delta = train_selected_e - train_comparator_e
                    test_delta = test_selected_e - test_comparator_e
                    train_sign = _sign(train_delta, neutral_epsilon_r)
                    test_sign = _sign(test_delta, neutral_epsilon_r)
                    agreement = (
                        train_sign != 0
                        and test_sign != 0
                        and train_sign == test_sign
                    )
                    if train_sign != 0 and test_sign != 0:
                        comparable_folds += 1
                        agreements += int(agreement)
                    loo_folds.append({
                        "holdoutSessionId": holdout_session,
                        "trainSelectedSamples": train_selected_n,
                        "trainComparatorSamples": train_comparator_n,
                        "holdoutSelectedSamples": test_selected_n,
                        "holdoutComparatorSamples": test_comparator_n,
                        "trainDeltaAllInR": train_delta,
                        "holdoutDeltaAllInR": test_delta,
                        "trainSign": train_sign,
                        "holdoutSign": test_sign,
                        "signAgreement": agreement,
                    })

                loo_agreement = (
                    agreements / comparable_folds
                    if comparable_folds > 0
                    else None
                )
                status = _effect_status(
                    pooled_delta=pooled_delta,
                    session_deltas=session_deltas,
                    loo_agreement_rate=loo_agreement,
                    comparison_sessions=len(session_effects),
                    loo_folds=len(loo_folds),
                    minimum_sessions=minimum_sessions,
                    minimum_effect_r=minimum_effect_r,
                    sign_agreement_rate=sign_agreement_rate,
                    max_session_share=_max_session_share(selected),
                )
                result.append({
                    "strategy": strategy,
                    "side": side,
                    "regime": regime,
                    "dimension": dimension,
                    "value": value,
                    "selectedSamples": selected_samples,
                    "comparatorSamples": comparator_samples,
                    "sessionsWithSelectedValue": len({
                        str(row.get("sessionId") or "")
                        for row in selected
                    }),
                    "selectedExpectancyAllInR": selected_expectancy,
                    "comparatorExpectancyAllInR": comparator_expectancy,
                    "pooledDeltaAllInR": pooled_delta,
                    "maxSessionSampleShare": _max_session_share(selected),
                    "comparisonSessions": len(session_effects),
                    "sessionPositiveEffects": sum(delta > neutral_epsilon_r for delta in session_deltas),
                    "sessionNegativeEffects": sum(delta < -neutral_epsilon_r for delta in session_deltas),
                    "sessionNeutralEffects": sum(abs(delta) <= neutral_epsilon_r for delta in session_deltas),
                    "medianSessionDeltaAllInR": _median(session_deltas),
                    "meanSessionDeltaAllInR": _mean(session_deltas),
                    "perSessionPooledSignAgreementRate": per_session_sign_rate,
                    "leaveOneSessionOutFolds": len(loo_folds),
                    "leaveOneSessionOutComparableFolds": comparable_folds,
                    "leaveOneSessionOutSignAgreementRate": loo_agreement,
                    "status": status,
                    "validationCandidate": status in {"stable_positive", "stable_negative"},
                    "livePolicyEligible": False,
                    "sessionEffects": session_effects,
                    "leaveOneSessionOut": loo_folds,
                })
    return result


def _threshold_effect(
    rows: list[dict],
    threshold: float,
) -> dict[str, Any] | None:
    known = [
        row
        for row in rows
        if (
            row.get("realizedAllInR") is not None
            and row.get("plannedNetRewardRisk") is not None
        )
    ]
    passed = [
        row for row in known
        if float(row["plannedNetRewardRisk"]) >= threshold
    ]
    failed = [
        row for row in known
        if float(row["plannedNetRewardRisk"]) < threshold
    ]
    pass_e, pass_n = _expectancy(passed)
    fail_e, fail_n = _expectancy(failed)
    base_e, base_n = _expectancy(known)
    if pass_e is None or fail_e is None:
        return {
            "threshold": threshold,
            "knownSamples": base_n,
            "passSamples": pass_n,
            "failSamples": fail_n,
            "passExpectancyAllInR": pass_e,
            "failExpectancyAllInR": fail_e,
            "baselineExpectancyAllInR": base_e,
            "passMinusFailAllInR": None,
            "passMinusBaselineAllInR": (
                pass_e - base_e
                if pass_e is not None and base_e is not None
                else None
            ),
        }
    return {
        "threshold": threshold,
        "knownSamples": base_n,
        "passSamples": pass_n,
        "failSamples": fail_n,
        "passExpectancyAllInR": pass_e,
        "failExpectancyAllInR": fail_e,
        "baselineExpectancyAllInR": base_e,
        "passMinusFailAllInR": pass_e - fail_e,
        "passMinusBaselineAllInR": (
            pass_e - base_e
            if base_e is not None
            else None
        ),
    }


def validate_fixed_rr_thresholds(
    trades: list[dict],
    *,
    thresholds: tuple[float, ...] = DEFAULT_RR_THRESHOLDS,
    minimum_sessions: int = 4,
    minimum_session_samples: int = 2,
    minimum_train_samples: int = 6,
    neutral_epsilon_r: float = 0.05,
    minimum_effect_r: float = 0.10,
    sign_agreement_rate: float = 0.75,
) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in trades:
        if (
            row.get("realizedAllInR") is None
            or row.get("plannedNetRewardRisk") is None
        ):
            continue
        groups[
            (
                str(row.get("strategy") or "unknown"),
                str(row.get("side") or "unknown"),
                str(row.get("regime") or "unknown"),
            )
        ].append(row)

    result: list[dict] = []
    for (strategy, side, regime), rows in sorted(groups.items()):
        session_ids = sorted({
            str(row.get("sessionId") or "")
            for row in rows
        })
        for threshold in thresholds:
            pooled = _threshold_effect(rows, threshold)
            if pooled is None:
                continue
            pooled_delta = pooled.get("passMinusFailAllInR")

            session_effects: list[dict] = []
            deltas: list[float] = []
            for session_id in session_ids:
                session_rows = [
                    row for row in rows
                    if str(row.get("sessionId") or "") == session_id
                ]
                effect = _threshold_effect(session_rows, threshold)
                if effect is None:
                    continue
                if (
                    int(effect.get("passSamples") or 0) < minimum_session_samples
                    or int(effect.get("failSamples") or 0) < minimum_session_samples
                    or effect.get("passMinusFailAllInR") is None
                ):
                    continue
                delta = float(effect["passMinusFailAllInR"])
                deltas.append(delta)
                session_effects.append({
                    "sessionId": session_id,
                    **effect,
                    "sign": _sign(delta, neutral_epsilon_r),
                })

            loo_folds: list[dict] = []
            comparable = 0
            agreements = 0
            for holdout in session_ids:
                train = [
                    row for row in rows
                    if str(row.get("sessionId") or "") != holdout
                ]
                test = [
                    row for row in rows
                    if str(row.get("sessionId") or "") == holdout
                ]
                train_effect = _threshold_effect(train, threshold)
                test_effect = _threshold_effect(test, threshold)
                if train_effect is None or test_effect is None:
                    continue
                if (
                    int(train_effect.get("passSamples") or 0) < minimum_train_samples
                    or int(train_effect.get("failSamples") or 0) < minimum_train_samples
                    or int(test_effect.get("passSamples") or 0) < minimum_session_samples
                    or int(test_effect.get("failSamples") or 0) < minimum_session_samples
                    or train_effect.get("passMinusFailAllInR") is None
                    or test_effect.get("passMinusFailAllInR") is None
                ):
                    continue
                train_delta = float(train_effect["passMinusFailAllInR"])
                test_delta = float(test_effect["passMinusFailAllInR"])
                train_sign = _sign(train_delta, neutral_epsilon_r)
                test_sign = _sign(test_delta, neutral_epsilon_r)
                agreement = (
                    train_sign != 0
                    and test_sign != 0
                    and train_sign == test_sign
                )
                if train_sign != 0 and test_sign != 0:
                    comparable += 1
                    agreements += int(agreement)
                loo_folds.append({
                    "holdoutSessionId": holdout,
                    "trainPassSamples": train_effect["passSamples"],
                    "trainFailSamples": train_effect["failSamples"],
                    "holdoutPassSamples": test_effect["passSamples"],
                    "holdoutFailSamples": test_effect["failSamples"],
                    "trainPassMinusFailAllInR": train_delta,
                    "holdoutPassMinusFailAllInR": test_delta,
                    "trainSign": train_sign,
                    "holdoutSign": test_sign,
                    "signAgreement": agreement,
                })

            loo_rate = (
                agreements / comparable
                if comparable > 0
                else None
            )
            status = _effect_status(
                pooled_delta=(
                    float(pooled_delta)
                    if pooled_delta is not None
                    else None
                ),
                session_deltas=deltas,
                loo_agreement_rate=loo_rate,
                comparison_sessions=len(session_effects),
                loo_folds=len(loo_folds),
                minimum_sessions=minimum_sessions,
                minimum_effect_r=minimum_effect_r,
                sign_agreement_rate=sign_agreement_rate,
                max_session_share=_max_session_share(rows),
            )
            result.append({
                "strategy": strategy,
                "side": side,
                "regime": regime,
                "threshold": threshold,
                **pooled,
                "comparisonSessions": len(session_effects),
                "sessionPositiveEffects": sum(delta > neutral_epsilon_r for delta in deltas),
                "sessionNegativeEffects": sum(delta < -neutral_epsilon_r for delta in deltas),
                "medianSessionPassMinusFailAllInR": _median(deltas),
                "leaveOneSessionOutFolds": len(loo_folds),
                "leaveOneSessionOutComparableFolds": comparable,
                "leaveOneSessionOutSignAgreementRate": loo_rate,
                "status": status,
                "validationCandidate": status == "stable_positive",
                "livePolicyEligible": False,
                "sessionEffects": session_effects,
                "leaveOneSessionOut": loo_folds,
            })
    return result


def validate_threshold_selection_holdout(
    trades: list[dict],
    *,
    thresholds: tuple[float, ...] = DEFAULT_RR_THRESHOLDS,
    minimum_sessions: int = 4,
    minimum_session_samples: int = 2,
    minimum_train_samples: int = 6,
    neutral_epsilon_r: float = 0.05,
    minimum_effect_r: float = 0.10,
    sign_agreement_rate: float = 0.75,
    threshold_consistency_rate: float = 0.50,
) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in trades:
        if (
            row.get("realizedAllInR") is None
            or row.get("plannedNetRewardRisk") is None
        ):
            continue
        groups[
            (
                str(row.get("strategy") or "unknown"),
                str(row.get("side") or "unknown"),
                str(row.get("regime") or "unknown"),
            )
        ].append(row)

    output: list[dict] = []
    for (strategy, side, regime), rows in sorted(groups.items()):
        session_ids = sorted({
            str(row.get("sessionId") or "")
            for row in rows
        })
        folds: list[dict] = []
        for holdout in session_ids:
            train = [
                row for row in rows
                if str(row.get("sessionId") or "") != holdout
            ]
            test = [
                row for row in rows
                if str(row.get("sessionId") or "") == holdout
            ]
            candidates = []
            for threshold in thresholds:
                effect = _threshold_effect(train, threshold)
                if effect is None:
                    continue
                if (
                    int(effect.get("passSamples") or 0) < minimum_train_samples
                    or int(effect.get("failSamples") or 0) < minimum_train_samples
                    or effect.get("passMinusFailAllInR") is None
                ):
                    continue
                candidates.append(effect)
            if not candidates:
                continue
            selected = max(
                candidates,
                key=lambda row: (
                    float(row.get("passMinusFailAllInR") or 0.0),
                    float(row.get("passExpectancyAllInR") or 0.0),
                    int(row.get("passSamples") or 0),
                    -float(row.get("threshold") or 0.0),
                ),
            )
            threshold = float(selected["threshold"])
            test_effect = _threshold_effect(test, threshold)
            if test_effect is None:
                continue
            if (
                int(test_effect.get("passSamples") or 0) < minimum_session_samples
                or int(test_effect.get("failSamples") or 0) < minimum_session_samples
                or test_effect.get("passMinusFailAllInR") is None
            ):
                continue
            holdout_delta = float(test_effect["passMinusFailAllInR"])
            folds.append({
                "holdoutSessionId": holdout,
                "selectedThreshold": threshold,
                "trainPassMinusFailAllInR": selected["passMinusFailAllInR"],
                "trainPassExpectancyAllInR": selected["passExpectancyAllInR"],
                "trainPassSamples": selected["passSamples"],
                "trainFailSamples": selected["failSamples"],
                "holdoutPassMinusFailAllInR": holdout_delta,
                "holdoutPassExpectancyAllInR": test_effect["passExpectancyAllInR"],
                "holdoutFailExpectancyAllInR": test_effect["failExpectancyAllInR"],
                "holdoutPassSamples": test_effect["passSamples"],
                "holdoutFailSamples": test_effect["failSamples"],
                "holdoutPositive": holdout_delta > neutral_epsilon_r,
            })

        deltas = [
            float(row["holdoutPassMinusFailAllInR"])
            for row in folds
        ]
        positive_rate = (
            sum(delta > neutral_epsilon_r for delta in deltas) / len(deltas)
            if deltas
            else None
        )
        threshold_counts = dict(Counter(
            f"{float(row['selectedThreshold']):.2f}"
            for row in folds
        ))
        threshold_mode_rate = (
            max(threshold_counts.values()) / len(folds)
            if folds and threshold_counts
            else None
        )
        median_delta = _median(deltas)
        if len(folds) < minimum_sessions:
            status = "insufficient_sessions"
        elif (
            positive_rate is not None
            and positive_rate >= sign_agreement_rate
            and median_delta is not None
            and median_delta >= minimum_effect_r
            and threshold_mode_rate is not None
            and threshold_mode_rate >= threshold_consistency_rate
        ):
            status = "stable_positive_holdout"
        elif (
            positive_rate is not None
            and positive_rate <= 1.0 - sign_agreement_rate
            and median_delta is not None
            and median_delta <= -minimum_effect_r
        ):
            status = "stable_negative_holdout"
        else:
            status = "unstable"

        output.append({
            "strategy": strategy,
            "side": side,
            "regime": regime,
            "sessions": len(session_ids),
            "folds": len(folds),
            "selectedThresholdCounts": threshold_counts,
            "selectedThresholdModeRate": threshold_mode_rate,
            "minimumThresholdConsistencyRate": threshold_consistency_rate,
            "holdoutPositiveRate": positive_rate,
            "meanHoldoutPassMinusFailAllInR": _mean(deltas),
            "medianHoldoutPassMinusFailAllInR": median_delta,
            "status": status,
            "validationCandidate": status == "stable_positive_holdout",
            "livePolicyEligible": False,
            "leaveOneSessionOut": folds,
        })
    return output


def _flatten_interactions(
    interactions: list[dict],
) -> dict[tuple, dict[str, Counter[str]]]:
    grouped: dict[tuple, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for row in interactions:
        session_id = str(row.get("sessionId") or "")
        strategy = str(row.get("strategy") or "unknown")
        state = str(row.get("state") or "unknown")
        side = str(row.get("hypothesisSide") or "unknown")
        forward = row.get("forward") or {}
        for horizon, horizon_data in forward.items():
            if not isinstance(horizon_data, dict):
                continue
            bands = horizon_data.get("movementBands") or {}
            for band, outcome in bands.items():
                if not isinstance(outcome, dict):
                    continue
                key = (
                    strategy,
                    state,
                    side,
                    str(horizon),
                    str(band),
                )
                classification = str(
                    outcome.get("classification") or "unknown"
                )
                grouped[key][session_id][classification] += 1
    return grouped


def validate_interaction_stability(
    interactions: list[dict],
    *,
    minimum_sessions: int = 4,
    minimum_resolved_per_session: int = 2,
    stable_rate: float = 0.70,
) -> list[dict]:
    grouped = _flatten_interactions(interactions)
    result = []
    for key, sessions in sorted(grouped.items()):
        session_rows = []
        total_hypothesis = 0
        total_opposite = 0
        for session_id, counts in sorted(sessions.items()):
            hypothesis = counts["hypothesis_first"]
            opposite = counts["opposite_first"]
            resolved = hypothesis + opposite
            total_hypothesis += hypothesis
            total_opposite += opposite
            if resolved < minimum_resolved_per_session:
                continue
            rate = hypothesis / resolved
            session_rows.append({
                "sessionId": session_id,
                "hypothesisFirst": hypothesis,
                "oppositeFirst": opposite,
                "resolved": resolved,
                "hypothesisSuccessRate": rate,
            })
        total_resolved = total_hypothesis + total_opposite
        pooled_rate = (
            total_hypothesis / total_resolved
            if total_resolved > 0
            else None
        )
        rates = [
            row["hypothesisSuccessRate"]
            for row in session_rows
        ]
        positive_sessions = sum(rate > 0.5 for rate in rates)
        opposite_sessions = sum(rate < 0.5 for rate in rates)
        if len(session_rows) < minimum_sessions:
            status = "insufficient_sessions"
        elif positive_sessions / len(session_rows) >= stable_rate:
            status = "stable_hypothesis_first"
        elif opposite_sessions / len(session_rows) >= stable_rate:
            status = "stable_opposite_first"
        else:
            status = "unstable"
        result.append({
            "strategy": key[0],
            "state": key[1],
            "side": key[2],
            "horizon": key[3],
            "band": key[4],
            "totalResolved": total_resolved,
            "hypothesisFirst": total_hypothesis,
            "oppositeFirst": total_opposite,
            "pooledHypothesisSuccessRate": pooled_rate,
            "comparisonSessions": len(session_rows),
            "medianSessionHypothesisSuccessRate": _median(rates),
            "meanSessionHypothesisSuccessRate": _mean(rates),
            "positiveSessions": positive_sessions,
            "oppositeSessions": opposite_sessions,
            "status": status,
            "validationCandidate": status in {
                "stable_hypothesis_first",
                "stable_opposite_first",
            },
            "sessionEffects": session_rows,
        })
    return result


def _opportunity_rows(
    opportunities: list[dict],
) -> dict[tuple[str, str, str], dict[str, list[dict]]]:
    grouped: dict[
        tuple[str, str, str],
        dict[str, list[dict]],
    ] = defaultdict(lambda: defaultdict(list))
    for item in opportunities:
        side = str(item.get("side") or "unknown")
        regime = str(item.get("entryLocalRegime") or "unknown")
        session_id = str(item.get("sessionId") or "")
        comparison = item.get("botComparison") or {}
        actual_trade = comparison.get("trade")
        actual_strategy = (
            str(actual_trade.get("strategy") or "")
            if isinstance(actual_trade, dict)
            else ""
        )
        bot_class = str(comparison.get("classification") or "unknown")
        fit = item.get("strategyFit") or {}
        for fit_row in fit.get("strategies") or []:
            strategy = str(fit_row.get("strategy") or "")
            if strategy not in TRADEABLE_PLAYBOOKS:
                continue
            fit_class = str(fit_row.get("fit") or "unaware")
            grouped[(strategy, side, regime)][session_id].append({
                "observed": fit_class in {"observed_aligned", "tradeable_aligned"},
                "tradeable": fit_class == "tradeable_aligned",
                "traded": (
                    actual_strategy == strategy
                    and bot_class not in {"missed", "wrong_direction"}
                ),
            })
    return grouped


def validate_hindsight_coverage_stability(
    opportunities: list[dict],
    *,
    minimum_sessions: int = 4,
    minimum_opportunities_per_session: int = 2,
) -> list[dict]:
    result = []
    for key, sessions in sorted(_opportunity_rows(opportunities).items()):
        session_rows = []
        for session_id, rows in sorted(sessions.items()):
            total = len(rows)
            if total < minimum_opportunities_per_session:
                continue
            observed = sum(bool(row["observed"]) for row in rows)
            tradeable = sum(bool(row["tradeable"]) for row in rows)
            traded = sum(bool(row["traded"]) for row in rows)
            session_rows.append({
                "sessionId": session_id,
                "opportunities": total,
                "observedCoverageRate": observed / total,
                "tradeableCoverageRate": tradeable / total,
                "tradeCoverageRate": traded / total,
            })
        observed_rates = [
            row["observedCoverageRate"]
            for row in session_rows
        ]
        tradeable_rates = [
            row["tradeableCoverageRate"]
            for row in session_rows
        ]
        trade_rates = [
            row["tradeCoverageRate"]
            for row in session_rows
        ]
        result.append({
            "strategy": key[0],
            "side": key[1],
            "regime": key[2],
            "comparisonSessions": len(session_rows),
            "sampleReady": len(session_rows) >= minimum_sessions,
            "meanObservedCoverageRate": _mean(observed_rates),
            "medianObservedCoverageRate": _median(observed_rates),
            "minObservedCoverageRate": min(observed_rates) if observed_rates else None,
            "maxObservedCoverageRate": max(observed_rates) if observed_rates else None,
            "meanTradeableCoverageRate": _mean(tradeable_rates),
            "medianTradeableCoverageRate": _median(tradeable_rates),
            "meanTradeCoverageRate": _mean(trade_rates),
            "medianTradeCoverageRate": _median(trade_rates),
            "sessionCoverage": session_rows,
        })
    return result


def build_stability_validation(
    *,
    trades: list[dict],
    hindsight: list[dict],
    interactions: list[dict],
    minimum_sessions: int = 4,
    minimum_session_samples: int = 2,
    minimum_train_samples: int = 6,
    minimum_interaction_resolved_per_session: int = 2,
    minimum_hindsight_opportunities_per_session: int = 2,
    neutral_epsilon_r: float = 0.05,
    minimum_effect_r: float = 0.10,
    sign_agreement_rate: float = 0.75,
    threshold_consistency_rate: float = 0.50,
) -> dict[str, Any]:
    feature = validate_feature_stability(
        trades,
        minimum_sessions=minimum_sessions,
        minimum_session_samples=minimum_session_samples,
        minimum_train_samples=minimum_train_samples,
        neutral_epsilon_r=neutral_epsilon_r,
        minimum_effect_r=minimum_effect_r,
        sign_agreement_rate=sign_agreement_rate,
    )
    fixed_rr = validate_fixed_rr_thresholds(
        trades,
        minimum_sessions=minimum_sessions,
        minimum_session_samples=minimum_session_samples,
        minimum_train_samples=minimum_train_samples,
        neutral_epsilon_r=neutral_epsilon_r,
        minimum_effect_r=minimum_effect_r,
        sign_agreement_rate=sign_agreement_rate,
    )
    selection = validate_threshold_selection_holdout(
        trades,
        minimum_sessions=minimum_sessions,
        minimum_session_samples=minimum_session_samples,
        minimum_train_samples=minimum_train_samples,
        neutral_epsilon_r=neutral_epsilon_r,
        minimum_effect_r=minimum_effect_r,
        sign_agreement_rate=sign_agreement_rate,
        threshold_consistency_rate=threshold_consistency_rate,
    )
    interactions_validation = validate_interaction_stability(
        interactions,
        minimum_sessions=minimum_sessions,
        minimum_resolved_per_session=minimum_interaction_resolved_per_session,
    )
    hindsight_validation = validate_hindsight_coverage_stability(
        hindsight,
        minimum_sessions=minimum_sessions,
        minimum_opportunities_per_session=minimum_hindsight_opportunities_per_session,
    )

    return {
        "schemaVersion": 1,
        "policy": {
            "minimumSessions": minimum_sessions,
            "minimumSessionSamples": minimum_session_samples,
            "minimumTrainSamples": minimum_train_samples,
            "minimumInteractionResolvedPerSession": minimum_interaction_resolved_per_session,
            "minimumHindsightOpportunitiesPerSession": minimum_hindsight_opportunities_per_session,
            "neutralEpsilonR": neutral_epsilon_r,
            "minimumEffectR": minimum_effect_r,
            "signAgreementRate": sign_agreement_rate,
            "thresholdConsistencyRate": threshold_consistency_rate,
            "livePolicyEnforcement": "disabled",
            "validationMethod": (
                "Session-level comparisons plus leave-one-session-out. "
                "No candidate in this report is automatically promoted "
                "to a live trading rule."
            ),
        },
        "summary": {
            "stablePositiveFeatureEffects": sum(
                row["status"] == "stable_positive"
                for row in feature
            ),
            "stableNegativeFeatureEffects": sum(
                row["status"] == "stable_negative"
                for row in feature
            ),
            "unstableFeatureEffects": sum(
                row["status"] == "unstable"
                for row in feature
            ),
            "stablePositiveFixedRrThresholds": sum(
                row["status"] == "stable_positive"
                for row in fixed_rr
            ),
            "stableThresholdSelectionGroups": sum(
                row["status"] == "stable_positive_holdout"
                for row in selection
            ),
            "stableInteractionHypotheses": sum(
                row["status"] == "stable_hypothesis_first"
                for row in interactions_validation
            ),
            "stableInteractionOppositions": sum(
                row["status"] == "stable_opposite_first"
                for row in interactions_validation
            ),
        },
        "featureEffects": feature,
        "fixedNetRewardRiskThresholds": fixed_rr,
        "thresholdSelectionHoldout": selection,
        "marketInteractionStability": interactions_validation,
        "hindsightCoverageStability": hindsight_validation,
    }
