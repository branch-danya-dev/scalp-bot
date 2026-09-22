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


def find_liquidity_targets(
    candles: list[Candle],
    entry: float,
    action: Action,
    *,
    min_distance_pct: float | None = None,
    max_distance_pct: float = 0.05,
    structure: "MarketStructure | None" = None,
) -> list[LiquidityTarget]:
    if not candles or entry <= 0 or action not in {Action.LONG, Action.SHORT}:
        return []

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
            if action == Action.LONG:
                eligible_kind = level.kind in {
                    "resistance",
                    "day_high",
                    "previous_day_high",
                }
                target_price = (
                    level.low
                    if level.kind == "resistance"
                    else level.center
                )
                distance = (target_price - entry) / entry
            else:
                eligible_kind = level.kind in {
                    "support",
                    "day_low",
                    "previous_day_low",
                }
                target_price = (
                    level.high
                    if level.kind == "support"
                    else level.center
                )
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
        return []
    candidates.sort(
        key=lambda target: (
            abs(target.price - entry) / entry,
            -target.score,
        )
    )
    deduped: list[LiquidityTarget] = []
    for candidate in candidates:
        if any(
            abs(candidate.price - row.price) / entry <= 0.00005
            for row in deduped
        ):
            continue
        deduped.append(candidate)
    return deduped


def find_liquidity_target(
    candles: list[Candle],
    entry: float,
    action: Action,
    *,
    min_distance_pct: float | None = None,
    max_distance_pct: float = 0.05,
    structure: "MarketStructure | None" = None,
) -> LiquidityTarget | None:
    targets = find_liquidity_targets(
        candles,
        entry,
        action,
        min_distance_pct=min_distance_pct,
        max_distance_pct=max_distance_pct,
        structure=structure,
    )
    return targets[0] if targets else None
