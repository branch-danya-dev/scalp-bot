from __future__ import annotations

from statistics import median

from .domain import Action, Candle, OrderBook, StrategyDecision, Trend


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


class Strategy:
    key: str
    label: str

    def evaluate(self, candles: list[Candle], book: OrderBook, trend: Trend) -> StrategyDecision:
        raise NotImplementedError


class TrendStructureStrategy(Strategy):
    key = "trend_structure"
    label = "Трендовая структура"

    def evaluate(self, candles: list[Candle], book: OrderBook, trend: Trend) -> StrategyDecision:
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
    label = "Горизонтальные уровни"

    def evaluate(self, candles: list[Candle], book: OrderBook, trend: Trend) -> StrategyDecision:
        if len(candles) < 60 or trend == Trend.FLAT:
            return StrategyDecision(self.key, Action.WAIT, ["Ждём тренд и повторно проверенный уровень"])
        close = candles[-1].close
        tolerance = 0.0018

        if trend == Trend.UP:
            lows = _swing_lows(candles[-120:])
            for _, level in reversed(lows[-8:]):
                touches = sum(1 for _, p in lows if abs(p - level) / level <= tolerance)
                if touches >= 2 and abs(close - level) / close <= 0.0025:
                    visuals = _price_visual("support", level, "support")
                    if _bullish_rejection(candles[-1]):
                        stop = min(candles[-1].low, level) * 0.999
                        risk = close - stop
                        return StrategyDecision(
                            strategy=self.key,
                            action=Action.LONG,
                            reasons=["Общий тренд вверх", f"Уровень подтверждён {touches} касаниями", "Цена дала отскок"],
                            confidence=0.72,
                            watched_level=level,
                            entry=close,
                            stop=stop,
                            target=close + risk * 1.7,
                            visuals=visuals,
                        )
                    return StrategyDecision(
                        self.key,
                        Action.WAIT,
                        ["Цена у поддержки, ждём подтверждение отскока"],
                        0.5,
                        level,
                        visuals=visuals,
                    )
        else:
            highs = _swing_highs(candles[-120:])
            for _, level in reversed(highs[-8:]):
                touches = sum(1 for _, p in highs if abs(p - level) / level <= tolerance)
                if touches >= 2 and abs(close - level) / close <= 0.0025:
                    visuals = _price_visual("resistance", level, "resistance")
                    if _bearish_rejection(candles[-1]):
                        stop = max(candles[-1].high, level) * 1.001
                        risk = stop - close
                        return StrategyDecision(
                            strategy=self.key,
                            action=Action.SHORT,
                            reasons=["Общий тренд вниз", f"Уровень подтверждён {touches} касаниями", "Цена дала отскок"],
                            confidence=0.72,
                            watched_level=level,
                            entry=close,
                            stop=stop,
                            target=close - risk * 1.7,
                            visuals=visuals,
                        )
                    return StrategyDecision(
                        self.key,
                        Action.WAIT,
                        ["Цена у сопротивления, ждём подтверждение отскока"],
                        0.5,
                        level,
                        visuals=visuals,
                    )

        return StrategyDecision(self.key, Action.WAIT, ["Рядом нет подтверждённого горизонтального уровня"])


class DensityBounceStrategy(Strategy):
    key = "orderbook_density"
    label = "Отскок от плотности"

    def evaluate(self, candles: list[Candle], book: OrderBook, trend: Trend) -> StrategyDecision:
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
