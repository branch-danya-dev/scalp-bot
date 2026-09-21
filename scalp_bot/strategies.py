from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median
from typing import Literal

from .domain import Action, Candle, OrderBook, StrategyDecision, TradeTick, Trend


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


def _swing_lows(candles: list[Candle], span: int = 2) -> list[tuple[int, float]]:
    result: list[tuple[int, float]] = []
    for i in range(span, len(candles) - span):
        value = candles[i].low
        if value <= min(x.low for x in candles[i - span : i]) and value <= min(
            x.low for x in candles[i + 1 : i + span + 1]
        ):
            result.append((i, value))
    return result


def _swing_highs(candles: list[Candle], span: int = 2) -> list[tuple[int, float]]:
    result: list[tuple[int, float]] = []
    for i in range(span, len(candles) - span):
        value = candles[i].high
        if value >= max(x.high for x in candles[i - span : i]) and value >= max(
            x.high for x in candles[i + 1 : i + span + 1]
        ):
            result.append((i, value))
    return result


def _typical_range_pct(candles: list[Candle]) -> float:
    values = [
        (c.high - c.low) / c.close
        for c in candles[-80:]
        if c.close > 0 and c.high >= c.low
    ]
    return median(values) if values else 0.001


def _typical_range_abs(candles: list[Candle]) -> float:
    values = [c.high - c.low for c in candles[-40:] if c.high >= c.low]
    return median(values) if values else (candles[-1].close * 0.001 if candles else 0)


def classify_trend(candles: list[Candle]) -> Trend:
    if len(candles) < 30:
        return Trend.FLAT
    window = candles[-80:]
    lows = _swing_lows(window)
    highs = _swing_highs(window)
    if len(lows) < 2 or len(highs) < 2:
        return Trend.FLAT
    higher_lows = lows[-1][1] > lows[-2][1]
    higher_highs = highs[-1][1] > highs[-2][1]
    lower_lows = lows[-1][1] < lows[-2][1]
    lower_highs = highs[-1][1] < highs[-2][1]
    if higher_lows and higher_highs:
        return Trend.UP
    if lower_lows and lower_highs:
        return Trend.DOWN
    return Trend.FLAT


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
    tolerance = min(max(_typical_range_pct(window) * 0.75, 0.0005), 0.003)
    swings = _swing_highs(window) if kind == "resistance" else _swing_lows(window)
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

    volume_baseline = median([c.volume for c in window if c.volume > 0]) if window else 0
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


def compute_trade_flow(trades: list[TradeTick], now_ms: int | None = None) -> dict:
    if not trades:
        return {
            "buyNotional5s": 0.0,
            "sellNotional5s": 0.0,
            "imbalance5s": 0.0,
            "notionalPerSecond5s": 0.0,
            "acceleration": 0.0,
            "tradeCount5s": 0,
        }
    if now_ms is None:
        now_ms = trades[-1].ts_ms

    recent_start = now_ms - 5_000
    previous_start = now_ms - 20_000
    recent = [t for t in trades if t.ts_ms >= recent_start]
    previous = [t for t in trades if previous_start <= t.ts_ms < recent_start]

    buy = sum(t.notional for t in recent if t.side.lower() == "buy")
    sell = sum(t.notional for t in recent if t.side.lower() == "sell")
    recent_total = buy + sell
    previous_total = sum(t.notional for t in previous)
    recent_rate = recent_total / 5
    previous_rate = previous_total / 15
    acceleration = recent_rate / previous_rate if previous_rate > 0 else (1.0 if recent_total > 0 else 0.0)

    return {
        "buyNotional5s": buy,
        "sellNotional5s": sell,
        "imbalance5s": (buy - sell) / recent_total if recent_total > 0 else 0.0,
        "notionalPerSecond5s": recent_rate,
        "acceleration": acceleration,
        "tradeCount5s": len(recent),
    }


def _bullish_rejection(candle: Candle) -> bool:
    body = abs(candle.close - candle.open)
    lower_wick = min(candle.open, candle.close) - candle.low
    return candle.close >= candle.open and lower_wick >= max(body * 0.8, candle.close * 0.00015)


def _bearish_rejection(candle: Candle) -> bool:
    body = abs(candle.close - candle.open)
    upper_wick = candle.high - max(candle.open, candle.close)
    return candle.close <= candle.open and upper_wick >= max(body * 0.8, candle.close * 0.00015)


def _trend_line_visual(window: list[Candle], first: tuple[int, float], second: tuple[int, float]) -> tuple[float, dict]:
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


def _price_visual(label: str, price: float, kind: str) -> dict:
    return {"overlays": [{"type": "price", "label": label, "price": price, "kind": kind}]}


def _zone_visual(zone: LevelZone, label: str | None = None) -> dict:
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


def _nearest_zone(
    zones: list[LevelZone],
    price: float,
    *,
    direction: Literal["above", "below"],
    max_distance_pct: float = 0.01,
) -> LevelZone | None:
    if direction == "above":
        eligible = [
            z for z in zones
            if z.high >= price * 0.999 and (z.low - price) / price <= max_distance_pct
        ]
        eligible.sort(key=lambda z: abs(z.center - price))
    else:
        eligible = [
            z for z in zones
            if z.low <= price * 1.001 and (price - z.high) / price <= max_distance_pct
        ]
        eligible.sort(key=lambda z: abs(z.center - price))
    return eligible[0] if eligible else None


class Strategy:
    key: str
    label: str

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
    ) -> StrategyDecision:
        raise NotImplementedError

    def reset(self, symbol: str) -> None:
        return None


class TrendStructureStrategy(Strategy):
    key = "trend_structure"
    label = "Трендовая структура"

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
    ) -> StrategyDecision:
        if len(candles) < 40 or trend == Trend.FLAT:
            return StrategyDecision(self.key, Action.WAIT, ["Нет читаемой трендовой структуры"])

        window = candles[-80:]
        close = candles[-1].close
        if trend == Trend.UP:
            lows = _swing_lows(window)
            if len(lows) < 2:
                return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных минимумов"])
            projected, visuals = _trend_line_visual(window, lows[-2], lows[-1])
            distance = abs(close - projected) / close
            if distance <= 0.0025 and _bullish_rejection(candles[-1]):
                stop = min(candles[-1].low, projected) * 0.999
                risk = close - stop
                return StrategyDecision(
                    strategy=self.key,
                    action=Action.LONG,
                    reasons=["Восходящая структура", "Откат к трендовой опоре", "Есть реакция покупателя"],
                    confidence=0.76,
                    watched_level=projected,
                    entry=close,
                    stop=stop,
                    target=close + risk * 1.6,
                    visuals=visuals,
                )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Тренд вверх, ждём откат к трендовой опоре"],
                0.4,
                projected,
                visuals=visuals,
            )

        highs = _swing_highs(window)
        if len(highs) < 2:
            return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных максимумов"])
        projected, visuals = _trend_line_visual(window, highs[-2], highs[-1])
        distance = abs(close - projected) / close
        if distance <= 0.0025 and _bearish_rejection(candles[-1]):
            stop = max(candles[-1].high, projected) * 1.001
            risk = stop - close
            return StrategyDecision(
                strategy=self.key,
                action=Action.SHORT,
                reasons=["Нисходящая структура", "Откат к трендовой опоре", "Есть реакция продавца"],
                confidence=0.76,
                watched_level=projected,
                entry=close,
                stop=stop,
                target=close - risk * 1.6,
                visuals=visuals,
            )
        return StrategyDecision(
            self.key,
            Action.WAIT,
            ["Тренд вниз, ждём откат к трендовой опоре"],
            0.4,
            projected,
            visuals=visuals,
        )


class HorizontalLevelStrategy(Strategy):
    key = "horizontal_levels"
    label = "Отбой от горизонтальной зоны"

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
    ) -> StrategyDecision:
        if len(candles) < 60 or trend == Trend.FLAT:
            return StrategyDecision(self.key, Action.WAIT, ["Ждём тренд и наторгованную ценовую зону"])

        close = candles[-1].close
        last = candles[-1]
        range_abs = _typical_range_abs(candles)

        if trend == Trend.UP:
            supports = detect_level_zones(candles, "support")
            zone = _nearest_zone(supports, close, direction="below")
            if zone is None:
                return StrategyDecision(self.key, Action.WAIT, ["Рядом нет наторгованной зоны поддержки"])
            visuals = _zone_visual(zone, "support zone")
            touched = last.low <= zone.high and last.high >= zone.low
            reclaimed = last.close >= zone.high
            if touched and reclaimed and _bullish_rejection(last):
                stop = zone.low - range_abs * 0.25
                risk = close - stop
                resistances = detect_level_zones(candles, "resistance")
                next_resistance = _nearest_zone(resistances, close, direction="above", max_distance_pct=0.03)
                target = (
                    next_resistance.low
                    if next_resistance and next_resistance.low > close
                    else close + risk * 1.5
                )
                return StrategyDecision(
                    strategy=self.key,
                    action=Action.LONG,
                    reasons=[
                        "Тренд вверх",
                        f"Зона поддержки наторгована: {zone.touches} касания",
                        "Цена проколола/коснулась зоны и вернулась выше неё",
                    ],
                    confidence=min(0.62 + zone.score * 0.025, 0.88),
                    watched_level=zone.center,
                    entry=close,
                    stop=stop,
                    target=target,
                    visuals=visuals,
                    details={"zone": zone.public(), "setup": "support_bounce"},
                )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Цена рядом с зоной поддержки, подтверждённого отбоя пока нет"],
                min(0.45 + zone.score * 0.015, 0.7),
                zone.center,
                visuals=visuals,
                details={"zone": zone.public(), "setup": "support_watch"},
            )

        resistances = detect_level_zones(candles, "resistance")
        zone = _nearest_zone(resistances, close, direction="above")
        if zone is None:
            return StrategyDecision(self.key, Action.WAIT, ["Рядом нет наторгованной зоны сопротивления"])
        visuals = _zone_visual(zone, "resistance zone")
        touched = last.high >= zone.low and last.low <= zone.high
        reclaimed = last.close <= zone.low
        if touched and reclaimed and _bearish_rejection(last):
            stop = zone.high + range_abs * 0.25
            risk = stop - close
            supports = detect_level_zones(candles, "support")
            next_support = _nearest_zone(supports, close, direction="below", max_distance_pct=0.03)
            target = (
                next_support.high
                if next_support and next_support.high < close
                else close - risk * 1.5
            )
            return StrategyDecision(
                strategy=self.key,
                action=Action.SHORT,
                reasons=[
                    "Тренд вниз",
                    f"Зона сопротивления наторгована: {zone.touches} касания",
                    "Цена проколола/коснулась зоны и вернулась ниже неё",
                ],
                confidence=min(0.62 + zone.score * 0.025, 0.88),
                watched_level=zone.center,
                entry=close,
                stop=stop,
                target=target,
                visuals=visuals,
                details={"zone": zone.public(), "setup": "resistance_bounce"},
            )
        return StrategyDecision(
            self.key,
            Action.WAIT,
            ["Цена рядом с зоной сопротивления, подтверждённого отбоя пока нет"],
            min(0.45 + zone.score * 0.015, 0.7),
            zone.center,
            visuals=visuals,
            details={"zone": zone.public(), "setup": "resistance_watch"},
        )


class DensityBounceStrategy(Strategy):
    key = "orderbook_density"
    label = "Отскок от плотности"

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
    ) -> StrategyDecision:
        if not candles or not book.bids or not book.asks or trend == Trend.FLAT:
            return StrategyDecision(self.key, Action.WAIT, ["Недостаточно данных стакана или нет тренда"])
        mid = book.mid
        if not mid:
            return StrategyDecision(self.key, Action.WAIT, ["Нет mid price"])

        bid_rows = [(p, q, p * q) for p, q in book.bids[:25]]
        ask_rows = [(p, q, p * q) for p, q in book.asks[:25]]
        notionals = [x[2] for x in bid_rows + ask_rows]
        baseline = median(notionals) if notionals else 0
        if baseline <= 0:
            return StrategyDecision(self.key, Action.WAIT, ["Стакан пуст"])

        bid_wall = max(bid_rows, key=lambda x: x[2])
        ask_wall = max(ask_rows, key=lambda x: x[2])
        close = candles[-1].close

        if trend == Trend.UP:
            distance = (mid - bid_wall[0]) / mid
            visuals = _price_visual("bid density", bid_wall[0], "density_bid")
            if 0 <= distance <= 0.003 and bid_wall[2] >= baseline * 4 and bid_wall[2] > ask_wall[2] * 1.15:
                stop = bid_wall[0] * 0.9988
                risk = close - stop
                if risk > 0:
                    return StrategyDecision(
                        strategy=self.key,
                        action=Action.LONG,
                        reasons=["Тренд вверх", "Крупная bid-плотность близко к цене", "Плотность заметно выше фона стакана"],
                        confidence=0.68,
                        watched_level=bid_wall[0],
                        entry=close,
                        stop=stop,
                        target=close + risk * 1.5,
                        visuals=visuals,
                    )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Тренд вверх, значимой bid-плотности рядом нет"],
                visuals=visuals,
            )

        distance = (ask_wall[0] - mid) / mid
        visuals = _price_visual("ask density", ask_wall[0], "density_ask")
        if 0 <= distance <= 0.003 and ask_wall[2] >= baseline * 4 and ask_wall[2] > bid_wall[2] * 1.15:
            stop = ask_wall[0] * 1.0012
            risk = stop - close
            if risk > 0:
                return StrategyDecision(
                    strategy=self.key,
                    action=Action.SHORT,
                    reasons=["Тренд вниз", "Крупная ask-плотность близко к цене", "Плотность заметно выше фона стакана"],
                    confidence=0.68,
                    watched_level=ask_wall[0],
                    entry=close,
                    stop=stop,
                    target=close - risk * 1.5,
                    visuals=visuals,
                )
        return StrategyDecision(
            self.key,
            Action.WAIT,
            ["Тренд вниз, значимой ask-плотности рядом нет"],
            visuals=visuals,
        )


DEFAULT_STRATEGIES: list[Strategy] = [
    TrendStructureStrategy(),
    HorizontalLevelStrategy(),
    DensityBounceStrategy(),
]
