"""Stable market-object identity shared by routing and the assigned playbook.

Only entry selection is scoped. The complete structure remains available for
obstacles and target construction. Legacy direct strategy calls have no binding.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


def _data(obj: Any) -> Mapping:
    return obj if isinstance(obj, Mapping) else obj.public()


def level_object_id(level: Any) -> str | None:
    value = _data(level).get("generation_id")
    return f"level:{value}" if value else None


def trendline_object_id(line: Any) -> str | None:
    data = _data(line)
    try:
        # Same anchors as TrendStructureStrategy._anchor_key; a changing
        # projected price / end timestamp alone is not a new market object.
        anchor = [str(data["kind"]), str(data["timeframe"]), int(data["start_ms"]),
                  round(float(data["start_price"]), 8), round(float(data["slope_per_bar"]), 10)]
    except (KeyError, TypeError, ValueError):
        return None
    return "trendline:" + json.dumps(anchor, separators=(",", ":"))


def candle_object_id(start_ms: Any) -> str | None:
    if isinstance(start_ms, int) and not isinstance(start_ms, bool) and start_ms >= 0:
        return f"candle:1m:{start_ms}"
    return None


def assigned_object_id(context: Any, owner: str) -> str | None:
    contract = getattr(context, "scenario", None)
    if isinstance(contract, Mapping) and contract.get("owner") == owner:
        return contract.get("marketObjectId")
    return None


def matches_assignment(context: Any, owner: str, object_id: str | None) -> bool:
    expected = assigned_object_id(context, owner)
    return expected is None or object_id == expected


def decision_object_id(decision: Any) -> str | None:
    """Read the object actually used, not a copy of the router's own label.

    Conflicting lifecycle / generation fields fail closed. The router must not
    be able to legitimize a foreign plan just by stamping its scenario on it.
    """
    details = decision.details or {}
    if decision.strategy in {"level_breakout", "weak_level_rejection"}:
        ids = set()
        lifecycle = details.get("levelLifecycle")
        if isinstance(lifecycle, Mapping):
            value = level_object_id(lifecycle)
            if value:
                ids.add(value)
        generation = details.get("levelGeneration")
        if generation:
            ids.add(f"level:{generation}")
        zone_generation = details.get("zoneGeneration")
        if isinstance(zone_generation, (list, tuple)) and len(zone_generation) == 2:
            ids.add(f"level:{zone_generation[1]}")
        return next(iter(ids)) if len(ids) == 1 else None
    if decision.strategy == "trend_structure":
        line = details.get("trendline")
        return trendline_object_id(line) if isinstance(line, Mapping) else None
    if decision.strategy == "price_action_hypothesis":
        hypothesis = details.get("hypothesis") or {}
        return candle_object_id(hypothesis.get("candleStartMs"))
    return None
