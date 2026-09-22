from __future__ import annotations

from dataclasses import dataclass
from math import floor, log

from ..domain import Candle
from .structure import MarketStructure, StructuralLevel


@dataclass(slots=True)
class LevelLife:
    level_id: str
    generation: int = 1
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    last_approach_ms: int | None = None
    distinct_approaches: int = 0
    dwell_bars: int = 0
    acceptance_bars: int = 0
    failed_breaks: int = 0
    sweeps: int = 0
    was_near: bool = False
    broken: bool = False
    last_counted_bar_ms: int | None = None


class LevelLifecycleTracker:
    def __init__(self) -> None:
        self._levels: dict[str, LevelLife] = {}

    @staticmethod
    def _id(level: StructuralLevel, reference_price: float) -> str:
        ratio = 1.0006
        bucket = floor(log(max(level.center, 1e-12)) / log(ratio))
        side = "S" if level.kind in {"support", "day_low", "previous_day_low"} else "R"
        return f"{side}:{bucket}"

    def update(
        self,
        structure: MarketStructure,
        candles: list[Candle],
        reference_price: float,
        now_ms: int,
    ) -> MarketStructure:
        if reference_price <= 0:
            return structure
        latest = candles[-1] if candles else None
        local_range = max(
            (c.high - c.low for c in candles[-20:]),
            default=reference_price * 0.001,
        )

        for level in structure.levels:
            level_id = self._id(level, reference_price)
            life = self._levels.get(level_id)
            if life is None:
                life = LevelLife(
                    level_id=level_id,
                    first_seen_ms=now_ms,
                    last_seen_ms=now_ms,
                    distinct_approaches=max(
                        1,
                        min(level.distinct_approaches or level.touches, 5),
                    ),
                    dwell_bars=level.dwell_bars,
                    acceptance_bars=level.acceptance_bars,
                )
                self._levels[level_id] = life
            elif life.broken and now_ms - life.last_seen_ms > 60_000:
                life.generation += 1
                life.broken = False
                life.distinct_approaches = 0
                life.dwell_bars = 0
                life.acceptance_bars = 0
                life.failed_breaks = 0
                life.sweeps = 0
                life.was_near = False
                life.first_seen_ms = now_ms
            life.last_seen_ms = now_ms

            tolerance = max(
                level.width * 0.5,
                local_range * 0.15,
                reference_price * 0.0004,
            )
            near = (
                reference_price >= level.low - tolerance
                and reference_price <= level.high + tolerance
            )
            if near and not life.was_near:
                life.distinct_approaches += 1
                life.last_approach_ms = now_ms
            life.was_near = near

            if latest is not None:
                overlaps = latest.high >= level.low and latest.low <= level.high
                closes_inside = level.low <= latest.close <= level.high
                if life.last_counted_bar_ms != latest.start_ms:
                    if overlaps:
                        life.dwell_bars += 1
                    if closes_inside:
                        life.acceptance_bars += 1

                    break_buffer = max(
                        level.width * 0.25,
                        reference_price * 0.0004,
                    )
                    if level.kind in {
                        "resistance",
                        "day_high",
                        "previous_day_high",
                    }:
                        pierced = latest.high > level.high + break_buffer
                        reclaimed = latest.close < level.low
                        accepted_through = latest.close > level.high + break_buffer
                    else:
                        pierced = latest.low < level.low - break_buffer
                        reclaimed = latest.close > level.high
                        accepted_through = latest.close < level.low - break_buffer
                    if pierced and reclaimed:
                        life.failed_breaks += 1
                        life.sweeps += 1
                    if accepted_through:
                        life.broken = True
                    life.last_counted_bar_ms = latest.start_ms

            level.level_id = level_id
            level.generation_id = f"{level_id}:g{life.generation}"
            level.distinct_approaches = life.distinct_approaches
            level.dwell_bars = life.dwell_bars
            level.acceptance_bars = life.acceptance_bars
            level.failed_breaks = life.failed_breaks
            level.sweeps = life.sweeps
            level.lifecycle = (
                "broken" if life.broken
                else "fresh" if life.distinct_approaches <= 1
                else "tested" if life.distinct_approaches <= 3
                else "worked"
            )

        stale = [
            key
            for key, life in self._levels.items()
            if now_ms - life.last_seen_ms > 3_600_000
        ]
        for key in stale:
            self._levels.pop(key, None)
        return structure
