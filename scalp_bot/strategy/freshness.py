from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from ..domain import Action, StrategyDecision


class EntryFreshnessClass(StrEnum):
    FRESH = "fresh"
    ACCEPTABLE = "acceptable"
    LATE = "late"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


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
    watched_level: float | None
    distance_from_level_pct: float | None
    source: str
    reasons: list[str]

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["classification"] = self.classification.value
        return data


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
    if (
        decision.action not in {Action.LONG, Action.SHORT}
        or current_price <= 0
        or trigger_price is None
        or trigger_price <= 0
    ):
        return EntryFreshness(
            classification=EntryFreshnessClass.UNKNOWN,
            trigger_price=trigger_price,
            current_price=current_price if current_price > 0 else None,
            trigger_ts=trigger_ts,
            observed_ts=observed_ts,
            confirmation_age_seconds=(
                max(0.0, observed_ts - trigger_ts)
                if trigger_ts is not None
                else None
            ),
            signed_move_since_trigger_pct=None,
            move_since_trigger_pct=None,
            expected_impulse_pct=None,
            move_spent_ratio=None,
            watched_level=decision.watched_level,
            distance_from_level_pct=None,
            source=source,
            reasons=["entry freshness lacks a directional trigger price"],
        )

    signed_move = _directional_move(
        decision.action,
        float(trigger_price),
        float(current_price),
    )
    favorable_move = max(0.0, signed_move)
    expected = _expected_impulse_pct(decision, current_price)
    spent = (
        favorable_move / expected
        if expected is not None and expected > 0
        else None
    )
    distance_from_level = (
        abs(float(current_price) - float(decision.watched_level))
        / float(current_price)
        if decision.watched_level is not None and current_price > 0
        else None
    )
    age = (
        max(0.0, observed_ts - trigger_ts)
        if trigger_ts is not None
        else None
    )

    if spent is None:
        classification = EntryFreshnessClass.UNKNOWN
        reasons.append("expected impulse could not be estimated")
    elif spent <= 0.25:
        classification = EntryFreshnessClass.FRESH
        reasons.append("no more than 25% of expected impulse is spent")
    elif spent <= 0.50:
        classification = EntryFreshnessClass.ACCEPTABLE
        reasons.append("25-50% of expected impulse is already spent")
    elif spent <= 0.80:
        classification = EntryFreshnessClass.LATE
        reasons.append("50-80% of expected impulse is already spent")
    else:
        classification = EntryFreshnessClass.EXHAUSTED
        reasons.append("more than 80% of expected impulse is already spent")

    if signed_move < 0:
        reasons.append("price is behind the causal trigger in trade direction")
    if age is not None:
        reasons.append(f"confirmation age is {age:.1f}s")

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
        move_spent_ratio=spent,
        watched_level=decision.watched_level,
        distance_from_level_pct=distance_from_level,
        source=source,
        reasons=reasons,
    )
