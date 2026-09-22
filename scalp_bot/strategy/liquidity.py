from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ..domain import Action, Candle
from .common import detect_level_zones, swing_highs, swing_lows, typical_range_pct

if TYPE_CHECKING:
    from .structure import MarketStructure


@dataclass(slots=True)
class LiquidityTarget:
    price: float
    kind: str
    touches: int
    score: float
    source_index: int | None = None

    def public(self) -> dict:
        return asdict(self)


def find_liquidity_target(
    candles: list[Candle],
    entry: float,
    action: Action,
    *,
    min_distance_pct: float | None = None,
    max_distance_pct: float = 0.05,
    structure: "MarketStructure | None" = None,
) -> LiquidityTarget | None:
    if not candles or entry <= 0 or action not in {Action.LONG, Action.SHORT}:
        return None

    local_range = typical_range_pct(candles)
    minimum = (
        max(0.0015, local_range * 0.60)
        if min_distance_pct is None
        else max(0.0, min_distance_pct)
    )
    candidates: list[LiquidityTarget] = []
    window = candles[-200:]

    if structure is not None:
        for level in structure.levels:
            target_price = level.center
            if action == Action.LONG:
                eligible_kind = level.kind in {"resistance", "day_high"}
                distance = (target_price - entry) / entry
            else:
                eligible_kind = level.kind in {"support", "day_low"}
                distance = (entry - target_price) / entry
            if eligible_kind and minimum <= distance <= max_distance_pct:
                candidates.append(
                    LiquidityTarget(
                        price=target_price,
                        kind=level.kind,
                        touches=level.touches,
                        score=4.0 + level.score * 6.0,
                        source_index=None,
                    )
                )

    if action == Action.LONG:
        for zone in detect_level_zones(candles, "resistance", min_touches=2):
            target_price = zone.low
            distance = (target_price - entry) / entry
            if minimum <= distance <= max_distance_pct:
                candidates.append(
                    LiquidityTarget(
                        price=target_price,
                        kind="resistance_zone",
                        touches=zone.touches,
                        score=2.0 + zone.touches + min(zone.score, 6.0),
                        source_index=zone.last_touch_index,
                    )
                )
        for index, target_price in swing_highs(window):
            distance = (target_price - entry) / entry
            if minimum <= distance <= max_distance_pct:
                candidates.append(
                    LiquidityTarget(
                        price=target_price,
                        kind="swing_high",
                        touches=1,
                        score=1.0 + index / max(len(window), 1),
                        source_index=index,
                    )
                )
    else:
        for zone in detect_level_zones(candles, "support", min_touches=2):
            target_price = zone.high
            distance = (entry - target_price) / entry
            if minimum <= distance <= max_distance_pct:
                candidates.append(
                    LiquidityTarget(
                        price=target_price,
                        kind="support_zone",
                        touches=zone.touches,
                        score=2.0 + zone.touches + min(zone.score, 6.0),
                        source_index=zone.last_touch_index,
                    )
                )
        for index, target_price in swing_lows(window):
            distance = (entry - target_price) / entry
            if minimum <= distance <= max_distance_pct:
                candidates.append(
                    LiquidityTarget(
                        price=target_price,
                        kind="swing_low",
                        touches=1,
                        score=1.0 + index / max(len(window), 1),
                        source_index=index,
                    )
                )

    if not candidates:
        return None
    candidates.sort(
        key=lambda target: (
            abs(target.price - entry) / entry,
            -target.score,
        )
    )
    return candidates[0]
