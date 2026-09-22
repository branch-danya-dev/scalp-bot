from __future__ import annotations

from dataclasses import dataclass
from time import time

from ..domain import Candle
from .common import clamp, typical_range_abs
from .structure import MarketStructure, StructuralLevel, build_market_structure


@dataclass(slots=True)
class LevelMemory:
    level_id: str
    generation: int
    kind: str
    center: float
    created_ms: int
    last_seen_ms: int
    consumed: bool = False
    broken_at_ms: int | None = None


def _normalize_kind(kind: str) -> str:
    if kind in {"support", "day_low", "previous_day_low", "rolling_24h_low"}:
        return "support"
    if kind in {"resistance", "day_high", "previous_day_high", "rolling_24h_high"}:
        return "resistance"
    return kind


def measure_interactions(
    candles: list[Candle],
    level: StructuralLevel,
) -> dict[str, int | str]:
    if not candles:
        return {
            "approaches": 0,
            "dwell": 0,
            "acceptance": 0,
            "rejections": 0,
            "sweeps": 0,
            "lifecycle": "fresh",
        }

    kind = _normalize_kind(level.kind)
    local_range = max(typical_range_abs(candles), candles[-1].close * 0.0004)
    near_buffer = max(level.width * 1.5, local_range * 0.60)
    departure_buffer = max(level.width * 2.0, local_range * 0.90)
    sweep_buffer = max(level.width * 0.25, local_range * 0.15)

    approaches = 0
    dwell = 0
    acceptance = 0
    rejections = 0
    sweeps = 0
    active_episode = False
    touched_episode = False

    rows = candles[-240:]
    for index, candle in enumerate(rows):
        near = (
            candle.high >= level.low - near_buffer
            and candle.low <= level.high + near_buffer
        )
        overlap = candle.high >= level.low and candle.low <= level.high
        close_inside = level.low <= candle.close <= level.high

        if near and not active_episode:
            approaches += 1
            active_episode = True
            touched_episode = False

        if overlap:
            dwell += 1
            touched_episode = True
        if close_inside:
            acceptance += 1

        if kind == "resistance":
            if candle.high > level.high + sweep_buffer and candle.close < level.high:
                sweeps += 1
            departed = candle.high < level.low - departure_buffer
        else:
            if candle.low < level.low - sweep_buffer and candle.close > level.low:
                sweeps += 1
            departed = candle.low > level.high + departure_buffer

        if active_episode and touched_episode and departed:
            rejections += 1
            active_episode = False
            touched_episode = False
        elif active_episode and not near:
            active_episode = False
            touched_episode = False

    recent = rows[-3:]
    if kind == "resistance":
        broken = len(recent) >= 2 and sum(c.close > level.high + sweep_buffer for c in recent) >= 2
    else:
        broken = len(recent) >= 2 and sum(c.close < level.low - sweep_buffer for c in recent) >= 2

    recent_acceptance = sum(
        1 for c in rows[-12:] if level.low <= c.close <= level.high
    )

    if broken:
        lifecycle = "broken"
    elif sweeps > 0 and rows and (
        (kind == "resistance" and rows[-1].close < level.high)
        or (kind == "support" and rows[-1].close > level.low)
    ):
        lifecycle = "swept"
    elif approaches <= 1 and acceptance <= 2:
        lifecycle = "fresh"
    elif approaches <= 3 and recent_acceptance <= 3:
        lifecycle = "tested"
    elif recent_acceptance >= 4:
        lifecycle = "weakened"
    else:
        lifecycle = "mature"

    return {
        "approaches": approaches,
        "dwell": dwell,
        "acceptance": acceptance,
        "rejections": rejections,
        "sweeps": sweeps,
        "lifecycle": lifecycle,
    }


class LevelEngine:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._memories: dict[str, LevelMemory] = {}
        self._counter = 0

    def _match(self, level: StructuralLevel, reference_price: float) -> LevelMemory | None:
        normalized = _normalize_kind(level.kind)
        tolerance = max(reference_price * 0.0010, level.width * 2.0, 1e-12)
        matches = [
            memory
            for memory in self._memories.values()
            if _normalize_kind(memory.kind) == normalized
            and abs(memory.center - level.center) <= tolerance
        ]
        if not matches:
            return None
        return min(matches, key=lambda memory: abs(memory.center - level.center))

    def _new_memory(self, level: StructuralLevel, now_ms: int) -> LevelMemory:
        self._counter += 1
        return LevelMemory(
            level_id=f"{self.symbol}:{_normalize_kind(level.kind)}:{self._counter}",
            generation=1,
            kind=level.kind,
            center=level.center,
            created_ms=level.created_ms or now_ms,
            last_seen_ms=now_ms,
        )

    def update(
        self,
        candles_1m: list[Candle],
        context_15m: list[Candle],
        reference_price: float,
    ) -> MarketStructure:
        structure = build_market_structure(candles_1m, context_15m, reference_price)
        now_ms = (
            candles_1m[-1].start_ms
            if candles_1m
            else int(time() * 1000)
        )

        for level in structure.levels:
            memory = self._match(level, reference_price or level.center or 1.0)
            metrics = measure_interactions(candles_1m, level)

            if memory is None:
                memory = self._new_memory(level, now_ms)
                self._memories[memory.level_id] = memory
            elif memory.broken_at_ms is not None:
                # A level that was genuinely broken may later reform at the
                # same price. A later touch starts a new generation.
                if level.last_touch_ms and level.last_touch_ms > memory.broken_at_ms + 60_000:
                    memory.generation += 1
                    memory.created_ms = level.last_touch_ms
                    memory.broken_at_ms = None
                    memory.consumed = False

            memory.kind = level.kind
            memory.center = level.center
            memory.last_seen_ms = now_ms

            lifecycle = str(metrics["lifecycle"])
            if lifecycle == "broken" and memory.broken_at_ms is None:
                memory.broken_at_ms = now_ms
            if memory.consumed and lifecycle not in {"broken", "swept"}:
                lifecycle = "consumed"

            level.level_id = memory.level_id
            level.generation = memory.generation
            level.approach_count = int(metrics["approaches"])
            level.dwell_bars = int(metrics["dwell"])
            level.acceptance_bars = int(metrics["acceptance"])
            level.rejection_count = int(metrics["rejections"])
            level.sweep_count = int(metrics["sweeps"])
            level.lifecycle = lifecycle
            level.created_ms = memory.created_ms

            # Structural score is neutral market information. Persistent
            # interaction evidence raises confidence without deciding whether
            # that is good for a bounce or a breakout.
            interaction_quality = clamp(
                level.approach_count / 6.0 * 0.35
                + level.rejection_count / 4.0 * 0.25
                + min(level.dwell_bars, 8) / 8.0 * 0.20
                + len(level.sources) / 4.0 * 0.20
            )
            level.score = clamp(max(level.score, interaction_quality))

        # Forget stale synthetic memories after a day; day high/low IDs will
        # naturally refresh with the next UTC day.
        cutoff = now_ms - 86_400_000
        self._memories = {
            key: memory
            for key, memory in self._memories.items()
            if memory.last_seen_ms >= cutoff
        }
        return structure

    def consume(self, level_id: str | None, generation: int | None = None) -> None:
        if not level_id:
            return
        memory = self._memories.get(level_id)
        if memory is None:
            return
        if generation is not None and memory.generation != generation:
            return
        memory.consumed = True

    def public_memory(self) -> list[dict]:
        return [
            {
                "levelId": item.level_id,
                "generation": item.generation,
                "kind": item.kind,
                "center": item.center,
                "consumed": item.consumed,
                "brokenAt": item.broken_at_ms,
            }
            for item in self._memories.values()
        ]
