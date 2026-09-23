from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..domain import Action, StrategyDecision, Trend


class LiquidityEvidenceState(StrEnum):
    NONE = "none"
    TRACKING = "tracking"
    ABSORBING = "absorbing"
    REPLENISHING = "replenishing"
    DEFENDED = "defended"
    CONSUMED = "consumed"
    REMOVED = "removed"
    LOST_SIGNIFICANCE = "lost_significance"
    UNKNOWN = "unknown"


class LiquidityAlignmentClass(StrEnum):
    SUPPORTIVE = "supportive"
    OPPOSED = "opposed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class LiquidityAlignment:
    classification: LiquidityAlignmentClass
    score: float | None
    reasons: list[str]

    def public(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "score": self.score,
            "reasons": list(self.reasons),
        }


@dataclass(slots=True)
class LiquidityEvidence:
    state: LiquidityEvidenceState
    wall_side: str | None
    wall_price: float | None
    source_state: str
    source_reason: str | None
    directional_bias: Trend
    directional_strength: float
    wall_present: bool | None
    remaining_ratio: float | None
    attack_ratio: float | None
    depletion_per_second: float | None
    replenishment_ratio: float | None
    absorption_observed: bool | None
    strength_multiple: float | None
    distance_pct: float | None
    lost_significance: bool
    consuming: bool
    consumption_causes: list[str]
    shadow_action: str | None
    reasons: list[str]

    def alignment_for(self, action: Action) -> LiquidityAlignment | None:
        if action not in {Action.LONG, Action.SHORT}:
            return None
        if self.state == LiquidityEvidenceState.UNKNOWN:
            return LiquidityAlignment(
                LiquidityAlignmentClass.UNKNOWN,
                None,
                ["liquidity status is outside observable book coverage"],
            )
        if self.directional_bias == Trend.FLAT:
            return LiquidityAlignment(
                LiquidityAlignmentClass.NEUTRAL,
                0.0,
                [f"liquidity state {self.state.value} has no directional veto"],
            )
        action_trend = Trend.UP if action == Action.LONG else Trend.DOWN
        if action_trend == self.directional_bias:
            return LiquidityAlignment(
                LiquidityAlignmentClass.SUPPORTIVE,
                self.directional_strength,
                [f"{self.state.value} liquidity supports {action.value}"],
            )
        return LiquidityAlignment(
            LiquidityAlignmentClass.OPPOSED,
            -self.directional_strength,
            [f"{self.state.value} liquidity opposes {action.value}"],
        )

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "wallSide": self.wall_side,
            "wallPrice": self.wall_price,
            "sourceState": self.source_state,
            "sourceReason": self.source_reason,
            "directionalBias": self.directional_bias.value,
            "directionalStrength": self.directional_strength,
            "wallPresent": self.wall_present,
            "remainingRatio": self.remaining_ratio,
            "attackRatio": self.attack_ratio,
            "depletionPerSecond": self.depletion_per_second,
            "replenishmentRatio": self.replenishment_ratio,
            "absorptionObserved": self.absorption_observed,
            "strengthMultiple": self.strength_multiple,
            "distancePct": self.distance_pct,
            "lostSignificance": self.lost_significance,
            "consuming": self.consuming,
            "consumptionCauses": list(self.consumption_causes),
            "shadowAction": self.shadow_action,
            "reasons": list(self.reasons),
        }


def _number(details: dict, key: str) -> float | None:
    value = details.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _consumption_causes(details: dict) -> list[str]:
    raw = details.get("consumptionCause")
    if isinstance(raw, list):
        return [str(value) for value in raw]
    causes: list[str] = []
    remaining = _number(details, "remainingRatio")
    depletion = _number(details, "depletionPerSecond")
    attack = _number(details, "attackRatio")
    if remaining is not None and remaining < 0.70:
        causes.append("remaining_ratio")
    if depletion is not None and depletion > 0.10:
        causes.append("depletion_rate")
    if attack is not None and attack >= 0.35 and (remaining or 0.0) < 0.85:
        causes.append("aggressive_attack")
    return causes


def build_liquidity_evidence(
    decision: StrategyDecision | None,
) -> LiquidityEvidence:
    if decision is None:
        return LiquidityEvidence(
            state=LiquidityEvidenceState.NONE,
            wall_side=None,
            wall_price=None,
            source_state="none",
            source_reason=None,
            directional_bias=Trend.FLAT,
            directional_strength=0.0,
            wall_present=None,
            remaining_ratio=None,
            attack_ratio=None,
            depletion_per_second=None,
            replenishment_ratio=None,
            absorption_observed=None,
            strength_multiple=None,
            distance_pct=None,
            lost_significance=False,
            consuming=False,
            consumption_causes=[],
            shadow_action=None,
            reasons=["no active density observation"],
        )

    details = decision.details or {}
    source_state = str(details.get("state") or "unknown")
    source_reason = (
        str(details.get("reason"))
        if details.get("reason") is not None
        else None
    )
    wall_side = (
        str(details.get("wallSide"))
        if details.get("wallSide") in {"bid", "ask"}
        else None
    )
    wall_price = _number(details, "wallPrice")
    wall_present_raw = details.get("wallPresent")
    wall_present = (
        bool(wall_present_raw)
        if isinstance(wall_present_raw, bool)
        else None
    )
    remaining = _number(details, "remainingRatio")
    attack = _number(details, "attackRatio")
    depletion = _number(details, "depletionPerSecond")
    replenishment = _number(details, "replenishmentRatio")
    strength = _number(details, "strengthMultiple")
    distance = _number(details, "distancePct")
    absorption = (
        bool(details.get("absorptionObserved"))
        if isinstance(details.get("absorptionObserved"), bool)
        else None
    )
    lost_significance = bool(details.get("lostSignificance"))
    consuming = bool(details.get("consuming"))
    causes = _consumption_causes(details)
    if causes:
        consuming = True

    if source_reason in {
        "wall_outside_book_coverage",
        "wall_exhausted_cooldown",
    }:
        state = LiquidityEvidenceState.UNKNOWN
    elif source_reason in {
        "wall_removed_before_defense",
        "wall_removed_after_defense",
    } or wall_present is False:
        state = LiquidityEvidenceState.REMOVED
    elif source_reason in {
        "wall_lost_significance",
        "wall_freshness_exhausted",
    } or lost_significance:
        state = LiquidityEvidenceState.LOST_SIGNIFICANCE
    elif source_reason == "wall_consumed_before_defense" or consuming:
        state = LiquidityEvidenceState.CONSUMED
    elif (
        replenishment is not None
        and replenishment >= 0.10
        and absorption is True
    ):
        state = LiquidityEvidenceState.REPLENISHING
    elif absorption is True:
        state = LiquidityEvidenceState.ABSORBING
    elif source_state in {"defended", "reaction"} and wall_side is not None:
        state = LiquidityEvidenceState.DEFENDED
    elif wall_side is not None and wall_price is not None:
        state = LiquidityEvidenceState.TRACKING
    elif source_state == "search":
        state = LiquidityEvidenceState.NONE
    else:
        state = LiquidityEvidenceState.UNKNOWN

    bias = Trend.FLAT
    directional_strength = 0.0
    reasons: list[str] = []

    if state in {
        LiquidityEvidenceState.ABSORBING,
        LiquidityEvidenceState.REPLENISHING,
        LiquidityEvidenceState.DEFENDED,
    } and wall_side is not None:
        bias = Trend.UP if wall_side == "bid" else Trend.DOWN
        base = {
            LiquidityEvidenceState.DEFENDED: 0.60,
            LiquidityEvidenceState.ABSORBING: 0.78,
            LiquidityEvidenceState.REPLENISHING: 0.85,
        }[state]
        directional_strength = base
        reasons.append(
            f"{wall_side} wall is {state.value}; bounce-side liquidity remains active"
        )
    elif state == LiquidityEvidenceState.CONSUMED and wall_side is not None:
        bias = Trend.DOWN if wall_side == "bid" else Trend.UP
        aggressive = "aggressive_attack" in causes
        directional_strength = 0.88 if aggressive else 0.72
        reasons.append(
            f"{wall_side} wall is being consumed; directional pressure is through the wall"
        )
    elif state == LiquidityEvidenceState.REMOVED:
        reasons.append(
            "displayed wall disappeared; removal is not treated as directional consumption"
        )
    elif state == LiquidityEvidenceState.LOST_SIGNIFICANCE:
        reasons.append(
            "wall is no longer significant relative to local book/activity"
        )
    elif state == LiquidityEvidenceState.TRACKING:
        reasons.append("wall is observable but has not produced a defended/consumed outcome")
    elif state == LiquidityEvidenceState.NONE:
        reasons.append("no qualifying wall is currently tracked")
    elif state == LiquidityEvidenceState.UNKNOWN:
        reasons.append("wall status cannot be inferred from current observable book")

    shadow_action = (
        decision.action.value
        if decision.action in {Action.LONG, Action.SHORT}
        else (
            str(details.get("shadowAction"))
            if details.get("shadowAction") in {"long", "short"}
            else None
        )
    )

    return LiquidityEvidence(
        state=state,
        wall_side=wall_side,
        wall_price=wall_price,
        source_state=source_state,
        source_reason=source_reason,
        directional_bias=bias,
        directional_strength=directional_strength,
        wall_present=wall_present,
        remaining_ratio=remaining,
        attack_ratio=attack,
        depletion_per_second=depletion,
        replenishment_ratio=replenishment,
        absorption_observed=absorption,
        strength_multiple=strength,
        distance_pct=distance,
        lost_significance=lost_significance,
        consuming=consuming,
        consumption_causes=causes,
        shadow_action=shadow_action,
        reasons=reasons,
    )
