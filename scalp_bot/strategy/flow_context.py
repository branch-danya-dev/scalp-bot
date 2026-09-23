from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..domain import Action, Trend


class FlowAlignmentClass(StrEnum):
    STRONGLY_ALIGNED = "strongly_aligned"
    ALIGNED = "aligned"
    SHORT_TERM_REVERSAL = "short_term_reversal"
    MIXED = "mixed"
    OPPOSED = "opposed"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(slots=True)
class FlowHorizon:
    seconds: int
    trade_imbalance: float
    cvd_usd: float
    trade_notional_usd: float
    trade_count: int
    ofi_usd: float
    normalized_ofi: float
    ofi_event_count: int
    score: float | None
    direction: Trend

    def public(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "tradeImbalance": self.trade_imbalance,
            "cvdUsd": self.cvd_usd,
            "tradeNotionalUsd": self.trade_notional_usd,
            "tradeCount": self.trade_count,
            "ofiUsd": self.ofi_usd,
            "normalizedOfi": self.normalized_ofi,
            "ofiEventCount": self.ofi_event_count,
            "score": self.score,
            "direction": self.direction.value,
        }


@dataclass(slots=True)
class FlowAlignment:
    classification: FlowAlignmentClass
    score: float | None
    aligned_horizons: list[int]
    opposed_horizons: list[int]
    neutral_horizons: list[int]
    reasons: list[str]

    def public(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "score": self.score,
            "alignedHorizons": list(self.aligned_horizons),
            "opposedHorizons": list(self.opposed_horizons),
            "neutralHorizons": list(self.neutral_horizons),
            "reasons": list(self.reasons),
        }


@dataclass(slots=True)
class MultiHorizonFlowContext:
    observed_at_ms: int
    horizons: dict[int, FlowHorizon]
    dominant_direction: Trend
    directional_score: float | None
    coherence: float
    long_alignment: FlowAlignment
    short_alignment: FlowAlignment

    def alignment_for(self, action: Action) -> FlowAlignment | None:
        if action == Action.LONG:
            return self.long_alignment
        if action == Action.SHORT:
            return self.short_alignment
        return None

    def public(self) -> dict[str, Any]:
        return {
            "observedAtMs": self.observed_at_ms,
            "dominantDirection": self.dominant_direction.value,
            "directionalScore": self.directional_score,
            "coherence": self.coherence,
            "horizons": {
                f"{seconds}s": horizon.public()
                for seconds, horizon in sorted(self.horizons.items())
            },
            "longAlignment": self.long_alignment.public(),
            "shortAlignment": self.short_alignment.public(),
        }


def _bounded(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _horizon(
    seconds: int,
    trade_flow: dict,
    book_flow: dict,
) -> FlowHorizon:
    suffix = f"{seconds}s"
    imbalance = float(trade_flow.get(f"imbalance{suffix}") or 0.0)
    cvd = float(trade_flow.get(f"cvd{suffix}") or 0.0)
    notional = float(trade_flow.get(f"notional{suffix}") or 0.0)
    trade_count = int(trade_flow.get(f"tradeCount{suffix}") or 0)
    ofi = float(book_flow.get(f"bestLevelOfiUsd{suffix}") or 0.0)
    normalized_ofi = float(book_flow.get(f"normalizedOfi{suffix}") or 0.0)
    ofi_event_count = int(book_flow.get(f"eventCount{suffix}") or 0)

    weighted = 0.0
    weight = 0.0
    if trade_count >= 3 and notional > 0:
        weighted += _bounded(imbalance) * 0.70
        weight += 0.70
    if ofi_event_count > 0:
        # 10% of visible top-5 depth is already material OFI. Scaling here is
        # diagnostic only; Stage 3 does not gate entries.
        weighted += _bounded(normalized_ofi / 0.10) * 0.30
        weight += 0.30

    score = weighted / weight if weight > 0 else None
    if score is None or abs(score) < 0.12:
        direction = Trend.FLAT
    else:
        direction = Trend.UP if score > 0 else Trend.DOWN

    return FlowHorizon(
        seconds=seconds,
        trade_imbalance=imbalance,
        cvd_usd=cvd,
        trade_notional_usd=notional,
        trade_count=trade_count,
        ofi_usd=ofi,
        normalized_ofi=normalized_ofi,
        ofi_event_count=ofi_event_count,
        score=score,
        direction=direction,
    )


def _alignment(
    horizons: dict[int, FlowHorizon],
    *,
    long_side: bool,
) -> FlowAlignment:
    side_sign = 1.0 if long_side else -1.0
    available = {
        seconds: horizon.score * side_sign
        for seconds, horizon in horizons.items()
        if horizon.score is not None
    }
    if not available:
        return FlowAlignment(
            classification=FlowAlignmentClass.INSUFFICIENT_DATA,
            score=None,
            aligned_horizons=[],
            opposed_horizons=[],
            neutral_horizons=[5, 15, 60],
            reasons=["no sufficiently populated trade-flow or OFI windows"],
        )

    aligned = [
        seconds for seconds, score in available.items()
        if score >= 0.12
    ]
    opposed = [
        seconds for seconds, score in available.items()
        if score <= -0.12
    ]
    neutral = [
        seconds for seconds, score in available.items()
        if -0.12 < score < 0.12
    ]
    weights = {5: 0.25, 15: 0.35, 60: 0.40}
    used_weight = sum(weights[seconds] for seconds in available)
    aggregate = (
        sum(
            available[seconds] * weights[seconds]
            for seconds in available
        )
        / used_weight
        if used_weight > 0
        else 0.0
    )

    reasons: list[str] = []
    score_5 = available.get(5)
    score_15 = available.get(15)
    score_60 = available.get(60)

    if (
        score_5 is not None
        and score_5 >= 0.12
        and (
            (score_15 is not None and score_15 <= -0.12)
            or (score_60 is not None and score_60 <= -0.12)
        )
    ):
        classification = FlowAlignmentClass.SHORT_TERM_REVERSAL
        reasons.append(
            "5s flow aligns with the trade while a longer horizon remains opposed"
        )
    elif (
        all(seconds in available for seconds in (5, 15, 60))
        and all(available[seconds] >= 0.12 for seconds in (5, 15, 60))
    ):
        classification = FlowAlignmentClass.STRONGLY_ALIGNED
        reasons.append("5s, 15s and 60s flow all align with the trade")
    elif (
        len(aligned) >= 2
        and aggregate >= 0.08
        and not (score_60 is not None and score_60 <= -0.12)
    ):
        classification = FlowAlignmentClass.ALIGNED
        reasons.append("at least two horizons align without 60s opposition")
    elif len(opposed) >= 2 and aggregate <= -0.08:
        classification = FlowAlignmentClass.OPPOSED
        reasons.append("multi-horizon flow is predominantly opposed to the trade")
    else:
        classification = FlowAlignmentClass.MIXED
        reasons.append("flow horizons do not agree on one directional hypothesis")

    return FlowAlignment(
        classification=classification,
        score=aggregate,
        aligned_horizons=sorted(aligned),
        opposed_horizons=sorted(opposed),
        neutral_horizons=sorted(neutral),
        reasons=reasons,
    )


def build_multi_horizon_flow_context(
    trade_flow: dict,
    book_flow: dict,
    *,
    observed_at_ms: int,
) -> MultiHorizonFlowContext:
    horizons = {
        seconds: _horizon(seconds, trade_flow, book_flow)
        for seconds in (5, 15, 60)
    }
    available = {
        seconds: horizon.score
        for seconds, horizon in horizons.items()
        if horizon.score is not None
    }
    weights = {5: 0.25, 15: 0.35, 60: 0.40}
    used_weight = sum(weights[seconds] for seconds in available)
    directional_score = (
        sum(
            available[seconds] * weights[seconds]
            for seconds in available
        )
        / used_weight
        if used_weight > 0
        else None
    )
    if directional_score is None or abs(directional_score) < 0.12:
        dominant = Trend.FLAT
    else:
        dominant = Trend.UP if directional_score > 0 else Trend.DOWN

    directional = [
        horizon.direction
        for horizon in horizons.values()
        if horizon.score is not None and horizon.direction != Trend.FLAT
    ]
    if not directional:
        coherence = 0.0
    else:
        up = sum(direction == Trend.UP for direction in directional)
        down = sum(direction == Trend.DOWN for direction in directional)
        coherence = max(up, down) / len(directional)

    return MultiHorizonFlowContext(
        observed_at_ms=observed_at_ms,
        horizons=horizons,
        dominant_direction=dominant,
        directional_score=directional_score,
        coherence=coherence,
        long_alignment=_alignment(horizons, long_side=True),
        short_alignment=_alignment(horizons, long_side=False),
    )
