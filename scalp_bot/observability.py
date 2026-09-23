from __future__ import annotations

from typing import Any

from .domain import Action, StrategyDecision, Trend


_WAITING_BY_STATE: dict[str, dict[str, list[str]]] = {
    "trend_structure": {
        "search": ["confirmed trend structure and a valid trendline"],
        "pullback": ["actual test of the trend support/resistance"],
        "test": ["reclaim of the trendline and aligned local tape"],
        "reclaim": ["follow-through in the trend direction"],
        "continuation": ["risk and execution approval"],
    },
    "weak_level_rejection": {
        "search": ["fresh/young horizontal level"],
        "found": ["directional approach to the level"],
        "approach": ["real break beyond the zone and reclaim"],
        "test": ["real break beyond the zone and reclaim"],
        "reject": ["fresh local tape reversal at the level"],
        "reaction": ["liquidity evidence propagation to active playbooks"],
    },
    "orderbook_density": {
        "search": ["significant observable order-book wall"],
        "persisting": ["wall persistence and stability"],
        "found": ["price approach to the wall"],
        "approach": ["actual trade touch of the wall"],
        "test": ["price reaction and fresh local tape reversal"],
        "defended": ["price reaction and fresh local tape reversal"],
        "reaction": ["risk and execution approval"],
    },
    "level_breakout": {
        "search": ["mature worked horizontal zone"],
        "found": ["directional pressure into the zone"],
        "approach": ["directional pressure into the zone"],
        "pressure": ["actual break of the zone"],
        "break": ["executed-flow acceptance beyond the broken edge"],
        "impulse": ["risk and execution approval"],
    },
}


def _market_object(decision: StrategyDecision) -> dict[str, Any]:
    details = decision.details or {}

    wall_price = details.get("wallPrice")
    if wall_price is not None:
        return {
            "type": "orderbook_wall",
            "label": f"{details.get('wallSide') or 'wall'} density",
            "price": wall_price,
            "side": details.get("wallSide"),
            "notionalUsd": details.get("notionalUsd"),
            "strengthMultiple": details.get("strengthMultiple"),
        }

    zone = details.get("zone")
    if isinstance(zone, dict):
        low = zone.get("low")
        high = zone.get("high")
        center = zone.get("center")
        if center is None and low is not None and high is not None:
            center = (float(low) + float(high)) / 2
        return {
            "type": "horizontal_zone",
            "label": zone.get("kind") or "level zone",
            "low": low,
            "high": high,
            "price": center,
            "touches": zone.get("touches"),
        }

    trendline = details.get("trendline")
    if isinstance(trendline, dict):
        return {
            "type": "trendline",
            "label": (
                f"{trendline.get('timeframe') or ''} "
                f"{trendline.get('kind') or 'trendline'}"
            ).strip(),
            "price": trendline.get("current_price"),
            "touches": trendline.get("touches"),
            "slopePerBar": trendline.get("slope_per_bar"),
        }

    if decision.watched_level is not None:
        return {
            "type": "price_level",
            "label": "watched level",
            "price": decision.watched_level,
        }

    return {"type": "market_context", "label": "market context"}


def _confirmed_facts(details: dict[str, Any]) -> list[str]:
    checks = [
        ("trendAligned", "trend direction aligned"),
        ("flowConfirmed", "flow confirmed"),
        ("pullbackDirectional", "directional pullback observed"),
        ("absorptionObserved", "absorption observed"),
        ("densityFresh", "density still fresh"),
        ("weakLevel", "weak/fresh level identified"),
    ]
    result = [
        label
        for key, label in checks
        if details.get(key) is True
    ]
    state = str(details.get("state") or "")
    if state in {"test", "reject", "defended", "reaction", "break", "impulse", "continuation"}:
        result.append(f"strategy progressed to {state}")
    return result


def _evidence(details: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "flow",
        "levelFlow",
        "recentLevelFlow",
        "breakoutFlow",
        "bookCoverage",
        "pullbackCharacter",
        "qualityFactors",
        "liquidityTarget",
        "targetSource",
        "positionInvalidated",
        "attackNotional5s",
        "attackRatio",
        "depletionPerSecond",
        "replenishmentRatio",
        "remainingRatio",
        "strengthMultiple",
        "entryFreshness",
        "multiHorizonFlow",
        "flowAlignment",
        "liquidityEvidence",
        "liquidityAlignment",
        "decisionContext",
    )
    return {
        key: details[key]
        for key in keys
        if key in details
    }


def build_decision_trace(
    decision: StrategyDecision,
    trend: Trend,
    observed_at_ms: int,
    *,
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = decision.details or {}
    state = str(details.get("state") or "unknown")
    waiting = list(
        _WAITING_BY_STATE
        .get(decision.strategy, {})
        .get(state, [])
    )
    if decision.action == Action.WAIT and decision.reasons:
        waiting.extend(
            reason
            for reason in decision.reasons
            if reason not in waiting
        )

    return {
        "observedAtMs": observed_at_ms,
        "strategy": decision.strategy,
        "action": decision.action.value,
        "state": state,
        "trend": trend.value,
        "marketContext": market_context,
        "setupId": decision.setup_id,
        "object": _market_object(decision),
        "confirmed": _confirmed_facts(details),
        "waitingFor": waiting,
        "reasons": list(decision.reasons),
        "confidence": decision.confidence,
        "entry": decision.entry,
        "stop": decision.stop,
        "target": decision.target,
        "evidence": _evidence(details),
    }



def build_trace_from_public(
    payload: dict[str, Any],
    observed_at_ms: int,
    trend_value: str | None = None,
) -> dict[str, Any]:
    action_raw = str(payload.get("action") or "wait")
    try:
        action = Action(action_raw)
    except ValueError:
        action = Action.WAIT
    trend_raw = trend_value or str(
        (payload.get("details") or {}).get("trend") or "flat"
    )
    try:
        trend = Trend(trend_raw)
    except ValueError:
        trend = Trend.FLAT

    decision = StrategyDecision(
        strategy=str(payload.get("strategy") or "unknown"),
        action=action,
        reasons=list(payload.get("reasons") or []),
        confidence=float(payload.get("confidence") or 0.0),
        watched_level=payload.get("watched_level"),
        entry=payload.get("entry"),
        stop=payload.get("stop"),
        target=payload.get("target"),
        visuals=dict(payload.get("visuals") or {}),
        details=dict(payload.get("details") or {}),
        setup_id=(
            payload.get("setup_id")
            or payload.get("setupId")
        ),
    )
    return build_decision_trace(
        decision,
        trend,
        observed_at_ms,
        market_context=(
            payload.get("marketContext")
            if isinstance(payload.get("marketContext"), dict)
            else None
        ),
    )
