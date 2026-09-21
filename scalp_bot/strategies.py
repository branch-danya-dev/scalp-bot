from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import floor, log10
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



def _nearby_round_level(price: float, tolerance_abs: float) -> float | None:
    if price <= 0:
        return None
    exponent = floor(log10(price))
    candidates: list[tuple[float, float]] = []
    for power in range(exponent - 3, exponent + 1):
        base = 10 ** power
        for multiplier in (1.0, 2.0, 2.5, 5.0, 10.0):
            step = base * multiplier
            relative_step = step / price
            if not 0.001 <= relative_step <= 0.05:
                continue
            rounded = round(price / step) * step
            candidates.append((abs(price - rounded), rounded))
    if not candidates:
        return None
    distance, rounded = min(candidates, key=lambda item: item[0])
    return rounded if distance <= max(tolerance_abs, price * 0.0005) else None


def _zone_overlap_count(candles: list[Candle], zone: LevelZone, lookback: int = 8) -> int:
    return sum(
        1
        for candle in candles[-lookback:]
        if candle.high >= zone.low and candle.low <= zone.high
    )


def _approach_is_directional(candles: list[Candle], kind: LevelKind) -> bool:
    if len(candles) < 5:
        return False
    recent = candles[-5:-1]
    closes = [c.close for c in recent]
    if kind == "resistance":
        return closes[-1] > closes[0] and sum(
            right >= left for left, right in zip(closes, closes[1:], strict=True)
        ) >= 2
    return closes[-1] < closes[0] and sum(
        right <= left for left, right in zip(closes, closes[1:], strict=True)
    ) >= 2

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


class RejectionStage(StrEnum):
    SEARCH = "search"
    FOUND = "found"
    APPROACH = "approach"
    TEST = "test"
    REJECT = "reject"
    REACTION = "reaction"


@dataclass(slots=True)
class RejectionWatchState:
    zone_key: tuple[str, float, float] | None = None
    stage: RejectionStage = RejectionStage.SEARCH


class WeakLevelRejectionStrategy(Strategy):
    key = "weak_level_rejection"
    label = "Отбой от слабого уровня"

    max_touches = 3
    approach_pct = 0.005
    max_stop_pct = 0.006

    def __init__(self) -> None:
        self._states: dict[str, RejectionWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    @staticmethod
    def _key(zone: LevelZone) -> tuple[str, float, float]:
        return (zone.kind, round(zone.low, 10), round(zone.high, 10))

    def _select_weak_zone(
        self,
        candles: list[Candle],
        price: float,
        kind: LevelKind,
    ) -> LevelZone | None:
        zones = detect_level_zones(candles, kind, lookback=100, min_touches=1)
        eligible: list[LevelZone] = []
        for zone in zones:
            if not 1 <= zone.touches <= self.max_touches:
                continue
            if abs(zone.center - price) / price > self.approach_pct:
                continue
            if len(candles) - 1 - zone.last_touch_index > 60:
                continue
            # A weak/new level should not already contain prolonged acceptance.
            if _zone_overlap_count(candles[:-1], zone, lookback=8) > 3:
                continue
            eligible.append(zone)
        eligible.sort(
            key=lambda zone: (
                abs(zone.center - price),
                zone.touches,
                -zone.last_touch_index,
            )
        )
        return eligible[0] if eligible else None

    @staticmethod
    def _trade_mode(action: Action, trend: Trend) -> tuple[str, bool]:
        with_trend = (
            (action == Action.LONG and trend == Trend.UP)
            or (action == Action.SHORT and trend == Trend.DOWN)
        )
        return (
            ("trend_following", True)
            if with_trend
            else ("countertrend_reaction", False)
        )

    def _decision_for_zone(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        trades: list[TradeTick],
        symbol: str,
        zone: LevelZone,
    ) -> StrategyDecision:
        state = self._states.setdefault(symbol, RejectionWatchState())
        zone_key = self._key(zone)
        if state.zone_key != zone_key:
            state.zone_key = zone_key
            state.stage = RejectionStage.FOUND

        last = candles[-1]
        price = book.mid or last.close
        flow = compute_trade_flow(trades)
        visuals = _zone_visual(zone, "weak rejection zone")
        approach = _approach_is_directional(candles, zone.kind)
        range_abs = _typical_range_abs(candles)

        if not approach:
            state.stage = RejectionStage.FOUND
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Слабый уровень найден, но направленного подхода к нему пока нет"],
                0.40,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "weakLevel": True,
                },
            )

        state.stage = RejectionStage.APPROACH
        round_level = _nearby_round_level(zone.center, max(range_abs * 0.5, zone.width))
        buffer = max(range_abs * 0.20, price * 0.00015)

        if zone.kind == "resistance":
            tested = last.high >= zone.low
            failed_break = last.high >= zone.high * 0.9995 and last.close < zone.low
            flow_reversed = flow["tradeCount5s"] >= 3 and flow["imbalance5s"] <= -0.05
            action = Action.SHORT
            stop_anchor = max(zone.high, round_level or zone.high)
            stop = stop_anchor + buffer
            risk = stop - price
        else:
            tested = last.low <= zone.high
            failed_break = last.low <= zone.low * 1.0005 and last.close > zone.high
            flow_reversed = flow["tradeCount5s"] >= 3 and flow["imbalance5s"] >= 0.05
            action = Action.LONG
            stop_anchor = min(zone.low, round_level or zone.low)
            stop = stop_anchor - buffer
            risk = price - stop

        if tested:
            state.stage = RejectionStage.TEST

        if not (tested and failed_break):
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Цена тестирует слабый уровень, ждём отказ от пробоя и возврат за зону"],
                0.50,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "roundLevel": round_level,
                    "weakLevel": True,
                },
            )

        state.stage = RejectionStage.REJECT
        if not flow_reversed:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Пробой не удержался, но поток сделок ещё не подтвердил отскок"],
                0.58,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "roundLevel": round_level,
                    "weakLevel": True,
                },
            )

        if risk <= 0:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["После возврата за уровень нет корректной точки инвалидации"],
                visuals=visuals,
            )

        stop_pct = risk / price
        if stop_pct > self.max_stop_pct:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Структурный stop за уровнем слишком далеко для скальпа"],
                0.45,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "roundLevel": round_level,
                    "stopDistancePct": stop_pct,
                    "weakLevel": True,
                },
            )

        trade_mode, allow_runner = self._trade_mode(action, trend)
        target_r = 1.6 if allow_runner else 1.25
        target = (
            price + risk * target_r
            if action == Action.LONG
            else price - risk * target_r
        )
        state.stage = RejectionStage.REACTION

        trend_reason = (
            "Отскок идёт по тренду: позицию можно вести после первого импульса"
            if allow_runner
            else "Отскок против тренда: берём только реакцию, runner запрещён"
        )
        confidence = 0.72 + min(zone.touches, 3) * 0.025
        if allow_runner:
            confidence += 0.05
        if round_level is not None:
            confidence += 0.03

        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                f"Слабый уровень: {zone.touches} подход(а), без длительной проторговки",
                "Попытка пробоя не удержалась, цена вернулась за границу зоны",
                "Поток исполненных сделок развернулся от уровня",
                trend_reason,
            ],
            confidence=min(confidence, 0.90),
            watched_level=zone.center,
            entry=price,
            stop=stop,
            target=target,
            visuals=visuals,
            details={
                "state": state.stage.value,
                "zone": zone.public(),
                "flow": flow,
                "roundLevel": round_level,
                "weakLevel": True,
                "tradeMode": trade_mode,
                "allowRunner": allow_runner,
                "exitMode": "runner_allowed" if allow_runner else "reaction_only",
            },
        )

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
    ) -> StrategyDecision:
        if len(candles) < 40 or trend == Trend.FLAT or not symbol:
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Нужен читаемый тренд и локальная история уровня"],
                details={"state": RejectionStage.SEARCH.value},
            )

        price = book.mid or candles[-1].close
        if price <= 0:
            return StrategyDecision(self.key, Action.WAIT, ["Нет текущей цены"])

        resistance = self._select_weak_zone(candles, price, "resistance")
        support = self._select_weak_zone(candles, price, "support")
        choices = [zone for zone in (resistance, support) if zone is not None]
        if not choices:
            self._states[symbol] = RejectionWatchState()
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Рядом нет молодого слабонаторгованного уровня"],
                details={"state": RejectionStage.SEARCH.value},
            )

        zone = min(choices, key=lambda item: abs(item.center - price))
        return self._decision_for_zone(
            candles,
            book,
            trend,
            trades or [],
            symbol,
            zone,
        )


class BreakoutStage(StrEnum):
    SEARCH = "search"
    FOUND = "found"
    APPROACH = "approach"
    PRESSURE = "pressure"
    BREAK = "break"
    IMPULSE = "impulse"


@dataclass(slots=True)
class BreakoutWatchState:
    zone_key: tuple[str, float, float] | None = None
    stage: BreakoutStage = BreakoutStage.SEARCH
    triggered: bool = False


class LevelBreakoutStrategy(Strategy):
    key = "level_breakout"
    label = "Пробой наторгованного уровня"

    approach_pct = 0.0045
    max_stop_pct = 0.006
    max_zone_distance_pct = 0.012

    def __init__(self) -> None:
        self._states: dict[str, BreakoutWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    @staticmethod
    def _key(zone: LevelZone) -> tuple[str, float, float]:
        return (zone.kind, round(zone.low, 10), round(zone.high, 10))

    @staticmethod
    def _select_zone(
        zones: list[LevelZone],
        price: float,
        *,
        long_side: bool,
    ) -> LevelZone | None:
        if long_side:
            eligible = [
                zone
                for zone in zones
                if zone.high >= price * 0.997
                and zone.low <= price * (1 + LevelBreakoutStrategy.max_zone_distance_pct)
            ]
        else:
            eligible = [
                zone
                for zone in zones
                if zone.low <= price * 1.003
                and zone.high >= price * (1 - LevelBreakoutStrategy.max_zone_distance_pct)
            ]
        eligible.sort(key=lambda zone: (abs(zone.center - price), -zone.score))
        return eligible[0] if eligible else None

    @staticmethod
    def _pressure_score(
        candles: list[Candle],
        zone: LevelZone,
        flow: dict,
        *,
        long_side: bool,
    ) -> tuple[int, dict]:
        recent = candles[-5:]
        if len(recent) < 4:
            return 0, {}

        if long_side:
            near_count = sum(1 for candle in recent if candle.close >= zone.low * 0.997)
            structure = sum(
                1 for left, right in zip(recent[-4:-1], recent[-3:], strict=True)
                if right.low >= left.low
            )
            flow_aligned = flow["imbalance5s"] >= 0.08
        else:
            near_count = sum(1 for candle in recent if candle.close <= zone.high * 1.003)
            structure = sum(
                1 for left, right in zip(recent[-4:-1], recent[-3:], strict=True)
                if right.high <= left.high
            )
            flow_aligned = flow["imbalance5s"] <= -0.08

        previous_volumes = [c.volume for c in candles[-25:-5] if c.volume > 0]
        baseline_volume = median(previous_volumes) if previous_volumes else 0.0
        recent_volume = sum(c.volume for c in recent[-3:]) / 3
        volume_active = baseline_volume > 0 and recent_volume >= baseline_volume

        score = 0
        score += int(near_count >= 2)
        score += int(structure >= 2)
        score += int(flow_aligned)
        score += int(flow["acceleration"] >= 1.1)
        score += int(volume_active)

        return score, {
            "nearCloses": near_count,
            "compressedPullbacks": structure,
            "flowAligned": flow_aligned,
            "volumeActive": volume_active,
        }

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
    ) -> StrategyDecision:
        if len(candles) < 60 or trend == Trend.FLAT or not symbol:
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Нет направленного контекста для пробоя"],
                details={"state": BreakoutStage.SEARCH.value},
            )

        trades = trades or []
        price = book.mid or candles[-1].close
        long_side = trend == Trend.UP
        zone_kind: LevelKind = "resistance" if long_side else "support"
        zones = detect_level_zones(candles, zone_kind)
        zone = self._select_zone(zones, price, long_side=long_side)

        if zone is None:
            self._states[symbol] = BreakoutWatchState()
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Натрогованная зона для пробоя рядом не найдена"],
                details={"state": BreakoutStage.SEARCH.value},
            )

        state = self._states.setdefault(symbol, BreakoutWatchState())
        zone_key = self._key(zone)
        if state.zone_key != zone_key:
            state.zone_key = zone_key
            state.stage = BreakoutStage.FOUND
            state.triggered = False

        flow = compute_trade_flow(trades)
        pressure_score, pressure = self._pressure_score(
            candles,
            zone,
            flow,
            long_side=long_side,
        )
        visuals = _zone_visual(zone, "breakout zone")

        if long_side:
            approach_distance = max(0.0, zone.low - price) / price
            returned_inside = price <= zone.high
        else:
            approach_distance = max(0.0, price - zone.high) / price
            returned_inside = price >= zone.low

        if state.triggered and returned_inside:
            state.triggered = False
            state.stage = BreakoutStage.APPROACH

        if approach_distance > self.approach_pct:
            state.stage = BreakoutStage.FOUND
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Натрогованная зона найдена, цена ещё не подошла"],
                0.42,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "pressure": pressure,
                    "pressureScore": pressure_score,
                },
            )

        state.stage = BreakoutStage.PRESSURE if pressure_score >= 2 else BreakoutStage.APPROACH
        break_buffer = max(0.00015, book.spread_pct * 1.5)
        broke = (
            price > zone.high * (1 + break_buffer)
            if long_side
            else price < zone.low * (1 - break_buffer)
        )

        if not broke:
            reason = (
                "Цена у уровня, давление на пробой сформировано"
                if state.stage == BreakoutStage.PRESSURE
                else "Цена подошла к уровню, ждём давление и триггер пробоя"
            )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [reason],
                min(0.48 + pressure_score * 0.05, 0.72),
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "pressure": pressure,
                    "pressureScore": pressure_score,
                },
            )

        state.stage = BreakoutStage.BREAK
        aligned_after_break = (
            flow["imbalance5s"] >= 0.05
            if long_side
            else flow["imbalance5s"] <= -0.05
        )
        if state.triggered:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Пробой этой зоны уже использован, повторно цену не догоняем"],
                0.35,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "pressure": pressure,
                    "pressureScore": pressure_score,
                },
            )

        if pressure_score < 2 or not aligned_after_break:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Зона проколота, но поток сделок пока не подтверждает импульс"],
                0.55,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "pressure": pressure,
                    "pressureScore": pressure_score,
                },
            )

        range_abs = _typical_range_abs(candles)
        entry = price
        if long_side:
            stop = zone.low - range_abs * 0.25
            expected_impulse = max(zone.width * 1.2, range_abs * 1.8)
            target = entry + expected_impulse
            stop_pct = (entry - stop) / entry
            action = Action.LONG
        else:
            stop = zone.high + range_abs * 0.25
            expected_impulse = max(zone.width * 1.2, range_abs * 1.8)
            target = entry - expected_impulse
            stop_pct = (stop - entry) / entry
            action = Action.SHORT

        if stop_pct <= 0 or stop_pct > self.max_stop_pct:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Пробой есть, но точка инвалидации слишком далеко для скальпа"],
                0.5,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "pressure": pressure,
                    "pressureScore": pressure_score,
                    "stopDistancePct": stop_pct,
                },
            )

        state.stage = BreakoutStage.IMPULSE
        state.triggered = True
        direction_reason = (
            "Поток сделок подтверждает агрессивного покупателя"
            if long_side
            else "Поток сделок подтверждает агрессивного продавца"
        )
        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                "Пробой наторгованной горизонтальной зоны",
                f"Зона подтверждена {zone.touches} касаниями и реакциями цены",
                direction_reason,
                "Цель сделки — забрать первый импульс, без пересиживания убытка",
            ],
            confidence=min(0.7 + pressure_score * 0.035 + min(zone.touches, 4) * 0.015, 0.92),
            watched_level=zone.center,
            entry=entry,
            stop=stop,
            target=target,
            visuals=visuals,
            details={
                "state": state.stage.value,
                "zone": zone.public(),
                "flow": flow,
                "pressure": pressure,
                "pressureScore": pressure_score,
                "stopDistancePct": stop_pct,
                "exitMode": "impulse_first",
            },
        )


DEFAULT_STRATEGIES: list[Strategy] = [
    TrendStructureStrategy(),
    HorizontalLevelStrategy(),
    WeakLevelRejectionStrategy(),
    DensityBounceStrategy(),
    LevelBreakoutStrategy(),
]
