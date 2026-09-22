from __future__ import annotations

from ..domain import Action, Candle, OrderBook, StrategyDecision, TradeTick, Trend
from .base import Strategy
from .common import (
    bearish_rejection,
    bullish_rejection,
    swing_highs,
    swing_lows,
    trend_line_visual,
)


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
            lows = swing_lows(window)
            if len(lows) < 2:
                return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных минимумов"])
            projected, visuals = trend_line_visual(window, lows[-2], lows[-1])
            distance = abs(close - projected) / close
            if distance <= 0.0025 and bullish_rejection(candles[-1]):
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
                    details={
                        "setupQuality": 0.76,
                        "tradeMode": "trend_following",
                        "allowRunner": True,
                    },
                )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Тренд вверх, ждём откат к трендовой опоре"],
                0.4,
                projected,
                visuals=visuals,
            )

        highs = swing_highs(window)
        if len(highs) < 2:
            return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных максимумов"])
        projected, visuals = trend_line_visual(window, highs[-2], highs[-1])
        distance = abs(close - projected) / close
        if distance <= 0.0025 and bearish_rejection(candles[-1]):
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
                details={
                    "setupQuality": 0.76,
                    "tradeMode": "trend_following",
                    "allowRunner": True,
                },
            )
        return StrategyDecision(
            self.key,
            Action.WAIT,
            ["Тренд вниз, ждём откат к трендовой опоре"],
            0.4,
            projected,
            visuals=visuals,
        )
