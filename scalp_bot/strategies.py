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
    lows = _swing_lows(candles[-80:])
    highs = _swing_highs(candles[-80:])
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

        close = candles[-1].close
        if trend == Trend.UP:
            lows = _swing_lows(candles[-80:])
            if len(lows) < 2:
                return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных минимумов"])
            (i1, p1), (i2, p2) = lows[-2], lows[-1]
            slope = (p2 - p1) / max(i2 - i1, 1)
            projected = p2 + slope * (len(candles[-80:]) - 1 - i2)
            distance = abs(close - projected) / close
            if distance <= 0.0025 and _bullish_rejection(candles[-1]):
                stop = min(candles[-1].low, projected) * 0.999
                risk = close - stop
                return StrategyDecision(
                    self.key,
                    Action.LONG,
                    ["Восходящая структура", "Откат к трендовой опоре", "Есть реакция покупателя"],
                    0.76,
                    projected,
                    close,
                    stop,
                    close + risk * 1.6,
                )
            return StrategyDecision(self.key, Action.WAIT, ["Тренд вверх, ждём откат к трендовой опоре"], 0.4, projected)

        highs = _swing_highs(candles[-80:])
        if len(highs) < 2:
            return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных максимумов"])
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        slope = (p2 - p1) / max(i2 - i1, 1)
        projected = p2 + slope * (len(candles[-80:]) - 1 - i2)
        distance = abs(close - projected) / close
        if distance <= 0.0025 and _bearish_rejection(candles[-1]):
            stop = max(candles[-1].high, projected) * 1.001
            risk = stop - close
            return StrategyDecision(
                self.key,
                Action.SHORT,
                ["Нисходящая структура", "Откат к трендовой опоре", "Есть реакция продавца"],
                0.76,
                projected,
                close,
                stop,
                close - risk * 1.6,
            )
        return StrategyDecision(self.key, Action.WAIT, ["Тренд вниз, ждём откат к трендовой опоре"], 0.4, projected)


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
                    if _bullish_rejection(candles[-1]):
                        stop = min(candles[-1].low, level) * 0.999
                        risk = close - stop
                        return StrategyDecision(
                            self.key,
                            Action.LONG,
                            ["Общий тренд вверх", f"Уровень подтверждён {touches} касаниями", "Цена дала отскок"],
                            0.72,
                            level,
                            close,
                            stop,
                            close + risk * 1.7,
                        )
                    return StrategyDecision(self.key, Action.WAIT, ["Цена у поддержки, ждём подтверждение отскока"], 0.5, level)
        else:
            highs = _swing_highs(candles[-120:])
            for _, level in reversed(highs[-8:]):
                touches = sum(1 for _, p in highs if abs(p - level) / level <= tolerance)
                if touches >= 2 and abs(close - level) / close <= 0.0025:
                    if _bearish_rejection(candles[-1]):
                        stop = max(candles[-1].high, level) * 1.001
                        risk = stop - close
                        return StrategyDecision(
                            self.key,
                            Action.SHORT,
                            ["Общий тренд вниз", f"Уровень подтверждён {touches} касаниями", "Цена дала отскок"],
                            0.72,
                            level,
                            close,
                            stop,
                            close - risk * 1.7,
                        )
                    return StrategyDecision(self.key, Action.WAIT, ["Цена у сопротивления, ждём подтверждение отскока"], 0.5, level)

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
            if 0 <= distance <= 0.003 and bid_wall[2] >= baseline * 4 and bid_wall[2] > ask_wall[2] * 1.15:
                stop = bid_wall[0] * 0.9988
                risk = close - stop
                if risk > 0:
                    return StrategyDecision(
                        self.key,
                        Action.LONG,
                        ["Тренд вверх", "Крупная bid-плотность близко к цене", "Плотность заметно выше фона стакана"],
                        0.68,
                        bid_wall[0],
                        close,
                        stop,
                        close + risk * 1.5,
                    )
            return StrategyDecision(self.key, Action.WAIT, ["Тренд вверх, значимой bid-плотности рядом нет"])

        distance = (ask_wall[0] - mid) / mid
        if 0 <= distance <= 0.003 and ask_wall[2] >= baseline * 4 and ask_wall[2] > bid_wall[2] * 1.15:
            stop = ask_wall[0] * 1.0012
            risk = stop - close
            if risk > 0:
                return StrategyDecision(
                    self.key,
                    Action.SHORT,
                    ["Тренд вниз", "Крупная ask-плотность близко к цене", "Плотность заметно выше фона стакана"],
                    0.68,
                    ask_wall[0],
                    close,
                    stop,
                    close - risk * 1.5,
                )
        return StrategyDecision(self.key, Action.WAIT, ["Тренд вниз, значимой ask-плотности рядом нет"])


DEFAULT_STRATEGIES: list[Strategy] = [
    TrendStructureStrategy(),
    HorizontalLevelStrategy(),
    DensityBounceStrategy(),
]
