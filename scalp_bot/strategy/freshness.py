from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..domain import Action, StrategyDecision


class EntryFreshnessClass(StrEnum):
    FRESH = "fresh"
    ACCEPTABLE = "acceptable"
    LATE = "late"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


DEFAULT_FRESHNESS_HORIZON_SECONDS = {
    "trend_structure": 90.0,
    "level_breakout": 120.0,
    "weak_level_rejection": 60.0,
    "orderbook_density": 60.0,
}


@dataclass(slots=True)
class EntryFreshness:
    classification: EntryFreshnessClass
    trigger_price: float | None
    current_price: float | None
    trigger_ts: float | None
    observed_ts: float
    confirmation_age_seconds: float | None
    signed_move_since_trigger_pct: float | None
    move_since_trigger_pct: float | None
    expected_impulse_pct: float | None
    move_spent_ratio: float | None
    expected_duration_seconds: float | None
    time_spent_ratio: float | None
    effective_spent_ratio: float | None
    watched_level: float | None
    distance_from_level_pct: float | None
    source: str
    reasons: list[str]

    def public(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "triggerPrice": self.trigger_price,
            "currentPrice": self.current_price,
            "triggerTs": self.trigger_ts,
            "observedTs": self.observed_ts,
            "confirmationAgeSeconds": self.confirmation_age_seconds,
            "signedMoveSinceTriggerPct": self.signed_move_since_trigger_pct,
            "moveSinceTriggerPct": self.move_since_trigger_pct,
            "expectedImpulsePct": self.expected_impulse_pct,
            "moveSpentRatio": self.move_spent_ratio,
            "expectedDurationSeconds": self.expected_duration_seconds,
            "timeSpentRatio": self.time_spent_ratio,
            "effectiveSpentRatio": self.effective_spent_ratio,
            "watchedLevel": self.watched_level,
            "distanceFromLevelPct": self.distance_from_level_pct,
            "source": self.source,
            "reasons": list(self.reasons),
        }


# Stage 13 uses the market opportunity as the freshness object.  Keep the old
# type/function names as compatibility aliases because reports and earlier
# research stages already consume "entryFreshness".
OpportunityFreshness = EntryFreshness
OpportunityFreshnessClass = EntryFreshnessClass


def _directional_move(
    action: Action,
    start: float,
    end: float,
) -> float:
    if start <= 0:
        return 0.0
    signed = (end - start) / start
    return signed if action == Action.LONG else -signed


def _expected_impulse_pct(
    decision: StrategyDecision,
    current_price: float,
) -> float | None:
    details = decision.details or {}
    explicit = details.get("expectedImpulsePct")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return float(explicit)

    entry = decision.entry
    target = decision.target
    stop = decision.stop
    if (
        isinstance(entry, (int, float))
        and entry > 0
        and isinstance(target, (int, float))
    ):
        target_move = abs(float(target) - float(entry)) / float(entry)
    else:
        target_move = 0.0

    risk_move = 0.0
    if (
        isinstance(entry, (int, float))
        and entry > 0
        and isinstance(stop, (int, float))
    ):
        risk_move = abs(float(entry) - float(stop)) / float(entry)

    target_r = details.get("targetR")
    if not isinstance(target_r, (int, float)):
        target_r = details.get("targetRiskMultipleGross")
    modeled = (
        risk_move * max(0.0, float(target_r))
        if isinstance(target_r, (int, float))
        else 0.0
    )

    resolved = max(target_move, modeled)
    return resolved if resolved > 0 else None


def _expected_duration_seconds(
    decision: StrategyDecision,
) -> float | None:
    details = decision.details or {}
    explicit = details.get("freshnessHorizonSeconds")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return float(explicit)
    return DEFAULT_FRESHNESS_HORIZON_SECONDS.get(
        decision.strategy,
        90.0,
    )


def _classification(
    ratio: float | None,
) -> tuple[EntryFreshnessClass, str]:
    if ratio is None:
        return (
            EntryFreshnessClass.UNKNOWN,
            "opportunity age and expected impulse could not be estimated",
        )
    if ratio <= 0.25:
        return (
            EntryFreshnessClass.FRESH,
            "no more than 25% of the opportunity budget is spent",
        )
    if ratio <= 0.50:
        return (
            EntryFreshnessClass.ACCEPTABLE,
            "25-50% of the opportunity budget is already spent",
        )
    if ratio <= 0.80:
        return (
            EntryFreshnessClass.LATE,
            "50-80% of the opportunity budget is already spent",
        )
    return (
        EntryFreshnessClass.EXHAUSTED,
        "more than 80% of the opportunity budget is already spent",
    )


def classify_entry_freshness(
    decision: StrategyDecision,
    *,
    trigger_price: float | None,
    trigger_ts: float | None,
    current_price: float,
    observed_ts: float,
    source: str,
) -> EntryFreshness:
    reasons: list[str] = []
    age = (
        max(0.0, observed_ts - trigger_ts)
        if trigger_ts is not None
        else None
    )
    duration = _expected_duration_seconds(decision)
    time_spent = (
        age / duration
        if age is not None and duration is not None and duration > 0
        else None
    )

    if (
        decision.action not in {Action.LONG, Action.SHORT}
        or current_price <= 0
        or trigger_price is None
        or trigger_price <= 0
    ):
        effective = time_spent
        classification, classification_reason = _classification(
            effective
        )
        if trigger_price is None or trigger_price <= 0:
            reasons.append(
                "opportunity freshness lacks a directional trigger price"
            )
        reasons.append(classification_reason)
        if age is not None:
            reasons.append(f"confirmation age is {age:.1f}s")
        return EntryFreshness(
            classification=classification,
            trigger_price=trigger_price,
            current_price=current_price if current_price > 0 else None,
            trigger_ts=trigger_ts,
            observed_ts=observed_ts,
            confirmation_age_seconds=age,
            signed_move_since_trigger_pct=None,
            move_since_trigger_pct=None,
            expected_impulse_pct=None,
            move_spent_ratio=None,
            expected_duration_seconds=duration,
            time_spent_ratio=time_spent,
            effective_spent_ratio=effective,
            watched_level=decision.watched_level,
            distance_from_level_pct=None,
            source=source,
            reasons=reasons,
        )

    signed_move = _directional_move(
        decision.action,
        float(trigger_price),
        float(current_price),
    )
    favorable_move = max(0.0, signed_move)
    expected = _expected_impulse_pct(decision, current_price)
    move_spent = (
        favorable_move / expected
        if expected is not None and expected > 0
        else None
    )
    available = [
        ratio
        for ratio in (move_spent, time_spent)
        if ratio is not None
    ]
    effective = max(available) if available else None
    classification, classification_reason = _classification(effective)
    reasons.append(classification_reason)

    distance_from_level = (
        abs(float(current_price) - float(decision.watched_level))
        / float(current_price)
        if decision.watched_level is not None and current_price > 0
        else None
    )

    if move_spent is None:
        reasons.append("expected impulse could not be estimated")
    if signed_move < 0:
        reasons.append("price is behind the causal trigger in trade direction")
    if age is not None:
        reasons.append(f"confirmation age is {age:.1f}s")
    if (
        time_spent is not None
        and move_spent is not None
        and time_spent > move_spent
    ):
        reasons.append("elapsed opportunity age is the binding freshness limit")

    return EntryFreshness(
        classification=classification,
        trigger_price=float(trigger_price),
        current_price=float(current_price),
        trigger_ts=trigger_ts,
        observed_ts=observed_ts,
        confirmation_age_seconds=age,
        signed_move_since_trigger_pct=signed_move,
        move_since_trigger_pct=favorable_move,
        expected_impulse_pct=expected,
        move_spent_ratio=move_spent,
        expected_duration_seconds=duration,
        time_spent_ratio=time_spent,
        effective_spent_ratio=effective,
        watched_level=decision.watched_level,
        distance_from_level_pct=distance_from_level,
        source=source,
        reasons=reasons,
    )


classify_opportunity_freshness = classify_entry_freshness
