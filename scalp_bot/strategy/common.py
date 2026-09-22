from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor, log10
from statistics import median
from typing import Literal

from ..domain import Action, Candle, TradeTick, Trend


LevelKind = Literal["support", "resistance"]


@dataclass(slots=True)
class LevelZone:
    kind: LevelKind
    low: float
    high: float
    touches: int
    reaction_pct: float
    volume_ratio: float
    score: float
    last_touch_index: int

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def width_pct(self) -> float:
        return self.width / self.center if self.center else 0.0

    def public(self) -> dict:
        return asdict(self)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def swing_lows(candles: list[Candle], span: int = 2) -> list[tuple[int, float]]:
    result: list[tuple[int, float]] = []
    for i in range(span, len(candles) - span):
        value = candles[i].low
        if value <= min(x.low for x in candles[i - span : i]) and value <= min(
            x.low for x in candles[i + 1 : i + span + 1]
        ):
            result.append((i, value))
    return result


def swing_highs(candles: list[Candle], span: int = 2) -> list[tuple[int, float]]:
    result: list[tuple[int, float]] = []
    for i in range(span, len(candles) - span):
        value = candles[i].high
        if value >= max(x.high for x in candles[i - span : i]) and value >= max(
            x.high for x in candles[i + 1 : i + span + 1]
        ):
            result.append((i, value))
    return result


def typical_range_pct(candles: list[Candle]) -> float:
    values = [
        (c.high - c.low) / c.close
        for c in candles[-80:]
        if c.close > 0 and c.high >= c.low
    ]
    return median(values) if values else 0.001


def typical_range_abs(candles: list[Candle]) -> float:
    values = [c.high - c.low for c in candles[-40:] if c.high >= c.low]
    return median(values) if values else (candles[-1].close * 0.001 if candles else 0.0)


def classify_trend(candles: list[Candle]) -> Trend:
    if len(candles) < 30:
        return Trend.FLAT
    window = candles[-80:]
    lows = swing_lows(window)
    highs = swing_highs(window)
    if len(lows) < 2 or len(highs) < 2:
        return Trend.FLAT
    if lows[-1][1] > lows[-2][1] and highs[-1][1] > highs[-2][1]:
        return Trend.UP
    if lows[-1][1] < lows[-2][1] and highs[-1][1] < highs[-2][1]:
        return Trend.DOWN
    return Trend.FLAT


def classify_context_trend(
    context_15m: list[Candle],
    context_1h: list[Candle],
) -> Trend:
    """Use 15m direction with 1h structure as an opposition veto."""
    intraday = classify_trend(context_15m)
    if intraday == Trend.FLAT:
        return Trend.FLAT
    higher = classify_trend(context_1h)
    if higher != Trend.FLAT and higher != intraday:
        return Trend.FLAT
    return intraday


def detect_level_zones(
    candles: list[Candle],
    kind: LevelKind,
    *,
    lookback: int = 160,
    min_touches: int = 2,
) -> list[LevelZone]:
    if len(candles) < 12:
        return []

    window = candles[-lookback:]
    offset = len(candles) - len(window)
    tolerance = min(max(typical_range_pct(window) * 0.75, 0.0005), 0.003)
    swings = swing_highs(window) if kind == "resistance" else swing_lows(window)
    if not swings:
        return []

    clusters: list[list[tuple[int, float]]] = []
    for index, price in swings:
        best: list[tuple[int, float]] | None = None
        best_distance = float("inf")
        for cluster in clusters:
            center = sum(x[1] for x in cluster) / len(cluster)
            distance = abs(price - center) / center if center else float("inf")
            if distance <= tolerance and distance < best_distance:
                best = cluster
                best_distance = distance
        if best is None:
            clusters.append([(index, price)])
        else:
            best.append((index, price))

    volume_baseline = median([c.volume for c in window if c.volume > 0]) if window else 0.0
    zones: list[LevelZone] = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        prices = [x[1] for x in cluster]
        center = sum(prices) / len(prices)
        padding = center * max(tolerance * 0.12, 0.00005)
        low = min(prices) - padding
        high = max(prices) + padding

        reactions: list[float] = []
        touch_volumes: list[float] = []
        for index, price in cluster:
            future = window[index + 1 : index + 7]
            if future:
                if kind == "resistance":
                    move = (price - min(c.low for c in future)) / price
                else:
                    move = (max(c.high for c in future) - price) / price
                reactions.append(max(0.0, move))
            touch_volumes.append(window[index].volume)

        reaction_pct = median(reactions) if reactions else 0.0
        avg_touch_volume = sum(touch_volumes) / len(touch_volumes)
        volume_ratio = avg_touch_volume / volume_baseline if volume_baseline > 0 else 1.0
        score = (
            len(cluster)
            + min(reaction_pct / max(tolerance, 1e-9), 4.0)
            + min(volume_ratio, 4.0) * 0.25
        )
        zones.append(
            LevelZone(
                kind=kind,
                low=low,
                high=high,
                touches=len(cluster),
                reaction_pct=reaction_pct,
                volume_ratio=volume_ratio,
                score=score,
                last_touch_index=offset + cluster[-1][0],
            )
        )

    zones.sort(key=lambda zone: (zone.score, zone.last_touch_index), reverse=True)
    return zones


def nearby_round_level(price: float, tolerance_abs: float) -> float | None:
    """Return only a genuinely nearby psychological round level.

    The old implementation accepted a very broad family of steps and used a
    widening tolerance, which made almost every weak-level setup report round
    number confluence. Here the candidate family is the conventional 1/2/5
    sequence and the final tolerance is capped at 3 bps of price.
    """
    if price <= 0:
        return None
    exponent = floor(log10(price))
    candidates: list[tuple[float, float]] = []
    for power in range(exponent - 4, exponent + 1):
        base = 10 ** power
        for multiplier in (1.0, 2.0, 5.0):
            step = base * multiplier
            relative_step = step / price
            if not 0.005 <= relative_step <= 0.05:
                continue
            rounded = round(price / step) * step
            candidates.append((abs(price - rounded), rounded))
    if not candidates:
        return None
    distance, rounded = min(candidates, key=lambda item: item[0])
    allowed = min(max(tolerance_abs, price * 0.00005), price * 0.0003)
    return rounded if distance <= allowed else None


def zone_overlap_count(candles: list[Candle], zone: LevelZone, lookback: int = 8) -> int:
    return sum(
        1
        for candle in candles[-lookback:]
        if candle.high >= zone.low and candle.low <= zone.high
    )


def approach_is_directional(candles: list[Candle], kind: LevelKind) -> bool:
    if len(candles) < 5:
        return False
    closes = [c.close for c in candles[-5:-1]]
    pairs = list(zip(closes, closes[1:]))
    if kind == "resistance":
        return closes[-1] > closes[0] and sum(right >= left for left, right in pairs) >= 2
    return closes[-1] < closes[0] and sum(right <= left for left, right in pairs) >= 2


def compute_trade_flow(trades: list[TradeTick], now_ms: int | None = None) -> dict:
    if not trades:
        return {
            "buyNotional5s": 0.0,
            "sellNotional5s": 0.0,
            "imbalance5s": 0.0,
            "notionalPerSecond5s": 0.0,
            "acceleration": 0.0,
            "tradeCount5s": 0,
            "previousTradeCount15s": 0,
            "tradeRateRatio": 0.0,
            "tradeSizeRatio": 0.0,
            "baselineReady": False,
            "participationConfirmed": False,
            "latestTradeAgeMs": None,
            "cvd5s": 0.0,
            "cvd15s": 0.0,
            "cvd60s": 0.0,
        }
    if now_ms is None:
        now_ms = trades[-1].ts_ms

    recent_start = now_ms - 5_000
    previous_start = now_ms - 20_000
    recent = [
        t
        for t in trades
        if recent_start <= t.ts_ms <= now_ms
    ]
    previous = [
        t
        for t in trades
        if previous_start <= t.ts_ms < recent_start
    ]

    buy = sum(t.notional for t in recent if t.side.lower() == "buy")
    sell = sum(t.notional for t in recent if t.side.lower() == "sell")
    recent_total = buy + sell
    previous_total = sum(t.notional for t in previous)
    recent_rate = recent_total / 5
    previous_rate = previous_total / 15
    recent_count_rate = len(recent) / 5
    previous_count_rate = len(previous) / 15
    recent_average = recent_total / len(recent) if recent else 0.0
    previous_average = (
        previous_total / len(previous)
        if previous
        else 0.0
    )
    acceleration = (
        recent_rate / previous_rate
        if previous_rate > 0
        else 0.0
    )
    trade_rate_ratio = (
        recent_count_rate / previous_count_rate
        if previous_count_rate > 0
        else 0.0
    )
    trade_size_ratio = (
        recent_average / previous_average
        if previous_average > 0
        else 0.0
    )
    baseline_ready = len(previous) >= 3 and previous_total > 0
    participation_confirmed = (
        baseline_ready
        and len(recent) >= 3
        and acceleration >= 1.0
    )

    from .flow import cumulative_delta

    latest_age_ms = max(0, now_ms - trades[-1].ts_ms)
    return {
        "buyNotional5s": buy,
        "sellNotional5s": sell,
        "imbalance5s": (buy - sell) / recent_total if recent_total > 0 else 0.0,
        "notionalPerSecond5s": recent_rate,
        "acceleration": acceleration,
        "tradeCount5s": len(recent),
        "previousTradeCount15s": len(previous),
        "tradeRateRatio": trade_rate_ratio,
        "tradeSizeRatio": trade_size_ratio,
        "baselineReady": baseline_ready,
        "participationConfirmed": participation_confirmed,
        "latestTradeAgeMs": latest_age_ms,
        "cvd5s": cumulative_delta(trades, 5, now_ms),
        "cvd15s": cumulative_delta(trades, 15, now_ms),
        "cvd60s": cumulative_delta(trades, 60, now_ms),
    }


def bullish_rejection(candle: Candle) -> bool:
    body = abs(candle.close - candle.open)
    lower_wick = min(candle.open, candle.close) - candle.low
    return candle.close >= candle.open and lower_wick >= max(body * 0.8, candle.close * 0.00015)


def bearish_rejection(candle: Candle) -> bool:
    body = abs(candle.close - candle.open)
    upper_wick = candle.high - max(candle.open, candle.close)
    return candle.close <= candle.open and upper_wick >= max(body * 0.8, candle.close * 0.00015)


def trend_line_visual(window: list[Candle], first: tuple[int, float], second: tuple[int, float]) -> tuple[float, dict]:
    i1, p1 = first
    i2, p2 = second
    slope = (p2 - p1) / max(i2 - i1, 1)
    projected = p2 + slope * (len(window) - 1 - i2)
    return projected, {
        "overlays": [
            {
                "type": "line",
                "label": "trend",
                "points": [
                    {"time": window[i1].start_ms // 1000, "price": p1},
                    {"time": window[-1].start_ms // 1000, "price": projected},
                ],
            }
        ]
    }


def price_visual(label: str, price: float, kind: str) -> dict:
    return {"overlays": [{"type": "price", "label": label, "price": price, "kind": kind}]}


def zone_visual(zone: LevelZone, label: str | None = None) -> dict:
    return {
        "overlays": [
            {
                "type": "zone",
                "label": label or zone.kind,
                "low": zone.low,
                "high": zone.high,
                "kind": zone.kind,
            }
        ]
    }


def trade_mode(action: Action, trend: Trend) -> tuple[str, bool]:
    with_trend = (
        (action == Action.LONG and trend == Trend.UP)
        or (action == Action.SHORT and trend == Trend.DOWN)
    )
    return ("trend_following", True) if with_trend else ("countertrend_reaction", False)
