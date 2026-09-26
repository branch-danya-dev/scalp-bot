"""Stable object identities shared by routing and playbooks, not signal scores."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

ObjectKind = Literal["structural_level", "trendline", "candle_pattern"]


@dataclass(frozen=True, slots=True)
class MarketObjectRef:
    kind: ObjectKind
    key: str

    def public(self) -> dict:
        return {"kind": self.kind, "key": self.key}

    @property
    def token(self) -> str:
        return json.dumps([self.kind, self.key], separators=(",", ":"))

    @classmethod
    def from_public(cls, value) -> MarketObjectRef | None:
        if not isinstance(value, dict):
            return None
        if value.get("kind") not in {"structural_level", "trendline", "candle_pattern"}:
            return None
        if not isinstance(value.get("key"), str) or not value["key"]:
            return None
        return cls(value["kind"], value["key"])


def level_ref(level) -> MarketObjectRef | None:
    raw = level if isinstance(level, dict) else level.public()
    # Generation is the existing playbook identity; a display/level id is not a substitute.
    key = raw.get("generation_id")
    return MarketObjectRef("structural_level", str(key)) if key else None


def trendline_anchor(line) -> tuple:
    raw = line if isinstance(line, dict) else line.public()
    return (raw["kind"], raw["timeframe"], raw["start_ms"],
            round(raw["start_price"], 8), round(raw["slope_per_bar"], 10))


def trendline_ref(line) -> MarketObjectRef:
    return MarketObjectRef("trendline", json.dumps(trendline_anchor(line), separators=(",", ":")))


def candle_ref(start_ms: int) -> MarketObjectRef:
    return MarketObjectRef("candle_pattern", str(start_ms))


def assigned_ref(context, owner: str) -> MarketObjectRef | None:
    raw = getattr(context, "scenario", None) or {}
    return MarketObjectRef.from_public(raw.get("objectRef")) if raw.get("owner") == owner else None


def decision_ref(decision) -> MarketObjectRef | None:
    """Derive identity from the owner's actual selected object, not a copied scenario tag."""
    details = decision.details or {}
    if decision.strategy in {"level_breakout", "weak_level_rejection"}:
        return level_ref(details.get("levelLifecycle") or {})
    if decision.strategy == "trend_structure":
        raw = details.get("trendline")
        if isinstance(raw, dict):
            try:
                return trendline_ref(raw)
            except (KeyError, TypeError, ValueError):
                return None
    if decision.strategy == "price_action_hypothesis":
        start = (details.get("hypothesis") or {}).get("candleStartMs")
        if isinstance(start, int):
            return candle_ref(start)
    return None
