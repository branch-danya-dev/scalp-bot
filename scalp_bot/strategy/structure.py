from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import combinations

from ..domain import Candle
from .common import (
    LevelKind,
    LevelZone,
    clamp,
    detect_level_zones,
    nearby_round_level,
    swing_highs,
    swing_lows,
    typical_range_abs,
    typical_range_pct,
)


@dataclass(slots=True)
class StructuralLevel:
    kind: str
    low: float
    high: float
    touches: int
    timeframe: str
    score: float
    reaction_pct: float = 0.0
    volume_ratio: float = 1.0
    last_touch_ms: int | None = None
    round_confluence: bool = False
    sources: list[str] = field(default_factory=list)

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2

    @property
    def width(self) -> float:
        return self.high - self.low

    def public(self) -> dict:
        data = asdict(self)
        data["center"] = self.center
        return data

    def as_zone(self) -> LevelZone:
        return LevelZone(
            kind="support" if "support" in self.kind or self.kind == "day_low" else "resistance",
            low=self.low,
            high=self.high,
            touches=self.touches,
            reaction_pct=self.reaction_pct,
            volume_ratio=self.volume_ratio,
            score=self.score * 10.0,
            last_touch_index=0,
        )


@dataclass(slots=True)
class TrendLine:
    kind: LevelKind
    timeframe: str
    start_ms: int
    end_ms: int
    start_price: float
    end_price: float
    current_price: float
    touches: int
    score: float
    slope_per_bar: float

    def public(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class MarketStructure:
    levels: list[StructuralLevel] = field(default_factory=list)
    trendlines: list[TrendLine] = field(default_factory=list)
    day_high: float | None = None
    day_low: float | None = None

    def public(self, max_levels: int = 14) -> dict:
        levels = sorted(self.levels, key=lambda item: item.score, reverse=True)[:max_levels]
        return {
            "levels": [item.public() for item in levels],
            "trendlines": [item.public() for item in self.trendlines],
            "dayHigh": self.day_high,
            "dayLow": self.day_low,
        }

    def nearest_horizontal(
        self,
        price: float,
        kind: LevelKind,
        *,
        max_distance_pct: float,
        min_touches: int = 1,
        max_touches: int | None = None,
    ) -> StructuralLevel | None:
        candidates = []
        for level in self.levels:
            if level.kind != kind:
                continue
            if level.touches < min_touches:
                continue
            if max_touches is not None and level.touches > max_touches:
                continue
            distance = abs(level.center - price) / price if price > 0 else 999.0
            if distance <= max_distance_pct:
                candidates.append((distance, -level.score, level))
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[0][2] if candidates else None

    def trendline(self, kind: LevelKind) -> TrendLine | None:
        rows = [line for line in self.trendlines if line.kind == kind]
        return max(rows, key=lambda line: line.score) if rows else None


def aggregate_candles(candles: list[Candle], interval_minutes: int) -> list[Candle]:
    if interval_minutes <= 1:
        return list(candles)
    bucket_ms = interval_minutes * 60_000
    grouped: dict[int, list[Candle]] = {}
    for candle in candles:
        bucket = candle.start_ms // bucket_ms
        grouped.setdefault(bucket, []).append(candle)

    result: list[Candle] = []
    for bucket in sorted(grouped):
        rows = grouped[bucket]
        result.append(
            Candle(
                start_ms=bucket * bucket_ms,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=sum(row.volume for row in rows),
                turnover=sum(row.turnover for row in rows),
                confirmed=all(row.confirmed for row in rows),
            )
        )
    return result


def _zone_level(zone: LevelZone, candles: list[Candle], timeframe: str) -> StructuralLevel:
    center = zone.center
    local_range = max(typical_range_pct(candles), 1e-9)
    touch_quality = clamp(zone.touches / 6.0)
    reaction_quality = clamp(zone.reaction_pct / (local_range * 2.0))
    volume_quality = clamp(zone.volume_ratio / 2.0)
    age = max(0, len(candles) - 1 - zone.last_touch_index)
    recency_quality = clamp(1.0 - age / max(len(candles), 1))
    tf_weight = {"1m": 0.85, "5m": 1.0, "15m": 1.10, "1h": 1.20}.get(timeframe, 1.0)
    score = clamp(
        (
            touch_quality * 0.38
            + reaction_quality * 0.28
            + volume_quality * 0.14
            + recency_quality * 0.20
        )
        * tf_weight
    )
    last_touch_ms = (
        candles[zone.last_touch_index].start_ms
        if 0 <= zone.last_touch_index < len(candles)
        else None
    )
    return StructuralLevel(
        kind=zone.kind,
        low=zone.low,
        high=zone.high,
        touches=zone.touches,
        timeframe=timeframe,
        score=score,
        reaction_pct=zone.reaction_pct,
        volume_ratio=zone.volume_ratio,
        last_touch_ms=last_touch_ms,
        round_confluence=nearby_round_level(center, center * 0.0003) is not None,
        sources=[timeframe],
    )


def _merge_levels(levels: list[StructuralLevel], reference_price: float) -> list[StructuralLevel]:
    if not levels:
        return []
    tolerance = max(reference_price * 0.0006, 1e-12)
    merged: list[StructuralLevel] = []
    for level in sorted(levels, key=lambda item: item.score, reverse=True):
        match = next(
            (
                existing
                for existing in merged
                if existing.kind == level.kind
                and abs(existing.center - level.center) <= tolerance
            ),
            None,
        )
        if match is None:
            merged.append(level)
            continue
        match.low = min(match.low, level.low)
        match.high = max(match.high, level.high)
        match.touches = max(match.touches, level.touches)
        match.score = clamp(max(match.score, level.score) + 0.05)
        match.reaction_pct = max(match.reaction_pct, level.reaction_pct)
        match.volume_ratio = max(match.volume_ratio, level.volume_ratio)
        match.round_confluence = match.round_confluence or level.round_confluence
        match.sources = sorted(set(match.sources + level.sources))
        if level.last_touch_ms and (
            match.last_touch_ms is None or level.last_touch_ms > match.last_touch_ms
        ):
            match.last_touch_ms = level.last_touch_ms
            match.timeframe = level.timeframe
    return merged


def _current_utc_day_extremes(context_15m: list[Candle]) -> tuple[float | None, float | None]:
    if not context_15m:
        return None, None
    latest = datetime.fromtimestamp(context_15m[-1].start_ms / 1000, tz=timezone.utc).date()
    rows = [
        candle
        for candle in context_15m
        if datetime.fromtimestamp(candle.start_ms / 1000, tz=timezone.utc).date() == latest
    ]
    if not rows:
        return None, None
    return max(c.high for c in rows), min(c.low for c in rows)


def _fit_line(points: list[tuple[int, float]]) -> tuple[float, float]:
    xs = [float(index) for index, _ in points]
    ys = [price for _, price in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return 0.0, mean_y
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    intercept = mean_y - slope * mean_x
    return slope, intercept


def detect_trendline(
    candles: list[Candle],
    kind: LevelKind,
    timeframe: str,
) -> TrendLine | None:
    if len(candles) < 30:
        return None
    pivots = swing_lows(candles) if kind == "support" else swing_highs(candles)
    pivots = pivots[-8:]
    if len(pivots) < 3:
        return None

    tolerance = max(typical_range_abs(candles) * 0.45, candles[-1].close * 0.0004)
    best: TrendLine | None = None
    for anchors in combinations(pivots, 3):
        if anchors[-1][0] - anchors[0][0] < 8:
            continue
        slope, intercept = _fit_line(list(anchors))
        residuals = [abs(price - (intercept + slope * index)) for index, price in pivots]
        touches = sum(error <= tolerance for error in residuals)
        if touches < 3:
            continue

        first_index = anchors[0][0]
        last_index = len(candles) - 1
        if kind == "support":
            violated = any(
                candle.low < (intercept + slope * index) - tolerance * 1.5
                for index, candle in enumerate(candles[first_index:], start=first_index)
            )
        else:
            violated = any(
                candle.high > (intercept + slope * index) + tolerance * 1.5
                for index, candle in enumerate(candles[first_index:], start=first_index)
            )
        if violated:
            continue

        mean_error = sum(error for error in residuals if error <= tolerance) / touches
        fit_quality = clamp(1.0 - mean_error / tolerance)
        recency = clamp(1.0 - (len(candles) - 1 - anchors[-1][0]) / len(candles))
        score = clamp(touches / 6.0 * 0.55 + fit_quality * 0.30 + recency * 0.15)
        line = TrendLine(
            kind=kind,
            timeframe=timeframe,
            start_ms=candles[first_index].start_ms,
            end_ms=candles[last_index].start_ms,
            start_price=intercept + slope * first_index,
            end_price=intercept + slope * last_index,
            current_price=intercept + slope * last_index,
            touches=touches,
            score=score,
            slope_per_bar=slope,
        )
        if best is None or line.score > best.score:
            best = line
    return best


def build_market_structure(
    candles_1m: list[Candle],
    context_15m: list[Candle],
    reference_price: float,
) -> MarketStructure:
    if reference_price <= 0 and candles_1m:
        reference_price = candles_1m[-1].close

    frames: list[tuple[str, list[Candle]]] = [
        ("1m", candles_1m[-240:]),
        ("5m", aggregate_candles(candles_1m[-240:], 5)),
        ("15m", context_15m[-120:]),
        ("1h", aggregate_candles(context_15m[-120:], 60)),
    ]

    levels: list[StructuralLevel] = []
    for timeframe, candles in frames:
        if len(candles) < 12:
            continue
        min_touches = 1 if timeframe == "1m" else 2
        for kind in ("support", "resistance"):
            zones = detect_level_zones(
                candles,
                kind,
                lookback=min(160, len(candles)),
                min_touches=min_touches,
            )
            levels.extend(_zone_level(zone, candles, timeframe) for zone in zones)

    levels = _merge_levels(levels, reference_price or 1.0)

    day_high, day_low = _current_utc_day_extremes(context_15m)
    if day_high is not None:
        levels.append(
            StructuralLevel(
                kind="day_high",
                low=day_high,
                high=day_high,
                touches=1,
                timeframe="1D",
                score=0.92,
                last_touch_ms=None,
                round_confluence=nearby_round_level(day_high, day_high * 0.0003) is not None,
                sources=["day_high"],
            )
        )
    if day_low is not None:
        levels.append(
            StructuralLevel(
                kind="day_low",
                low=day_low,
                high=day_low,
                touches=1,
                timeframe="1D",
                score=0.92,
                last_touch_ms=None,
                round_confluence=nearby_round_level(day_low, day_low * 0.0003) is not None,
                sources=["day_low"],
            )
        )

    trendlines: list[TrendLine] = []
    for timeframe, candles in frames[:2]:
        for kind in ("support", "resistance"):
            line = detect_trendline(candles, kind, timeframe)
            if line is not None:
                trendlines.append(line)

    return MarketStructure(
        levels=sorted(levels, key=lambda item: item.score, reverse=True),
        trendlines=sorted(trendlines, key=lambda item: item.score, reverse=True)[:4],
        day_high=day_high,
        day_low=day_low,
    )
