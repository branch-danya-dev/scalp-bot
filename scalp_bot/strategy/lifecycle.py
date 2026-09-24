from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor, log
from statistics import median

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
    departed_since_ms: int | None = None
    last_approach_bar_ms: int | None = None
    broken: bool = False
    last_counted_bar_ms: int | None = None


class LevelLifecycleTracker:
    # Distinct approaches describe separate market visits, not sub-second
    # oscillation around a zone boundary. A level must first move materially
    # away, remain away for a short interval, and return on a later confirmed
    # candle before lifecycle maturity increases.
    approach_departure_seconds = 5.0
    approach_min_separation_seconds = 15.0
    approach_reset_multiple = 2.0

    def __init__(self) -> None:
        self._levels: dict[str, LevelLife] = {}

    @staticmethod
    def _id(
        level: StructuralLevel,
        reference_price: float,
        now_ms: int,
    ) -> str:
        side = (
            "S"
            if level.kind in {
                "support",
                "day_low",
                "previous_day_low",
            }
            else "R"
        )
        object_kind = str(level.kind or "unknown")
        if level.kind in {"day_high", "day_low"}:
            session_date = datetime.fromtimestamp(
                now_ms / 1000,
                tz=timezone.utc,
            ).date().isoformat()
            return f"{side}:{object_kind}:{session_date}"

        ratio = 1.0006
        bucket = floor(
            log(max(level.center, 1e-12)) / log(ratio)
        )
        # Price buckets stabilize detector drift, but structurally different
        # market objects must never share one generation. Current-day
        # extremes instead keep one causal identity for the UTC session so
        # extending the same impulse cannot manufacture a new setup merely
        # because the session high/low moved.
        return f"{side}:{object_kind}:{bucket}"

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
        range_values = [
            c.high - c.low
            for c in candles[-20:]
            if c.high >= c.low
        ]
        local_range = (
            median(range_values)
            if range_values
            else reference_price * 0.001
        )

        for level in structure.levels:
            level_id = self._id(level, reference_price, now_ms)
            life = self._levels.get(level_id)
            if life is None:
                historical_approaches = max(
                    1,
                    min(level.distinct_approaches or level.touches, 5),
                )
                tolerance = max(
                    level.width * 0.5,
                    local_range * 0.15,
                    reference_price * 0.0004,
                )
                currently_near = (
                    reference_price >= level.low - tolerance
                    and reference_price <= level.high + tolerance
                )
                life = LevelLife(
                    level_id=level_id,
                    first_seen_ms=now_ms,
                    last_seen_ms=now_ms,
                    distinct_approaches=historical_approaches,
                    dwell_bars=level.dwell_bars,
                    acceptance_bars=level.acceptance_bars,
                    was_near=currently_near,
                    departed_since_ms=(
                        None
                        if currently_near
                        else now_ms
                    ),
                    last_approach_ms=(now_ms if currently_near else None),
                    last_approach_bar_ms=(
                        latest.start_ms
                        if currently_near and latest is not None
                        else None
                    ),
                    last_counted_bar_ms=(
                        latest.start_ms
                        if latest is not None
                        and (level.dwell_bars > 0 or level.acceptance_bars > 0)
                        else None
                    ),
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
                life.departed_since_ms = now_ms
                life.last_approach_bar_ms = None
                life.first_seen_ms = now_ms
            life.last_seen_ms = now_ms

            tolerance = max(
                level.width * 0.5,
                local_range * 0.15,
                reference_price * 0.0004,
            )
            reset_distance = max(
                tolerance * self.approach_reset_multiple,
                level.width,
                reference_price * 0.0008,
            )
            near = (
                reference_price >= level.low - tolerance
                and reference_price <= level.high + tolerance
            )
            materially_away = (
                reference_price < level.low - reset_distance
                or reference_price > level.high + reset_distance
            )

            if materially_away:
                if life.was_near or life.departed_since_ms is None:
                    life.departed_since_ms = now_ms
                life.was_near = False
            elif near:
                if not life.was_near:
                    departed_long_enough = (
                        life.departed_since_ms is not None
                        and now_ms - life.departed_since_ms
                        >= int(
                            self.approach_departure_seconds
                            * 1000
                        )
                    )
                    separated_in_time = (
                        life.last_approach_ms is None
                        or now_ms - life.last_approach_ms
                        >= int(
                            self.approach_min_separation_seconds
                            * 1000
                        )
                    )
                    separated_by_bar = (
                        latest is None
                        or life.last_approach_bar_ms is None
                        or latest.start_ms
                        != life.last_approach_bar_ms
                    )
                    if (
                        departed_long_enough
                        and separated_in_time
                        and separated_by_bar
                    ):
                        life.distinct_approaches += 1
                        life.last_approach_ms = now_ms
                        life.last_approach_bar_ms = (
                            latest.start_ms
                            if latest is not None
                            else None
                        )
                life.was_near = True
                life.departed_since_ms = None
            # Between the near and reset bands, keep the previous hysteresis
            # state. Merely crossing the near threshold is not a new approach.

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
            level.first_seen_ms = life.first_seen_ms
            level.last_seen_ms = life.last_seen_ms
            level.last_approach_ms = life.last_approach_ms
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
