from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain import Action, Candle, OrderBook, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
    from .structure import MarketStructure
from .common import (
    bearish_rejection,
    bullish_rejection,
    swing_highs,
    swing_lows,
    trend_line_visual,
)
from .liquidity import find_liquidity_target


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
        structure: "MarketStructure | None" = None,
    ) -> StrategyDecision:
        if len(candles) < 40 or trend == Trend.FLAT:
            return StrategyDecision(self.key, Action.WAIT, ["Нет читаемой трендовой структуры"])

        window = candles[-80:]
        close = candles[-1].close
        if trend == Trend.UP:
            structural_line = structure.trendline("support") if structure else None
            if structural_line is not None:
                projected = structural_line.current_price
                visuals = {
                    "overlays": [{
                        "type": "line",
                        "label": "trend support",
                        "points": [
                            {"time": structural_line.start_ms // 1000, "price": structural_line.start_price},
                            {"time": structural_line.end_ms // 1000, "price": structural_line.end_price},
                        ],
                    }]
                }
                distance = abs(close - projected) / close
                if distance <= 0.0025 and bullish_rejection(candles[-1]):
                    stop = min(candles[-1].low, projected) * 0.999
                    risk = close - stop
                    liquidity_target = find_liquidity_target(
                        candles, close, Action.LONG, structure=structure
                    )
                    target = liquidity_target.price if liquidity_target else close + risk * 1.6
                    quality = min(0.92, 0.65 + structural_line.score * 0.25)
                    return StrategyDecision(
                        strategy=self.key,
                        action=Action.LONG,
                        reasons=[
                            "Восходящая структура",
                            f"Касание наклонной поддержки ({structural_line.touches} опор)",
                            "Есть реакция покупателя",
                        ],
                        confidence=quality,
                        watched_level=projected,
                        entry=close,
                        stop=stop,
                        target=target,
                        visuals=visuals,
                        details={
                            "setupQuality": quality,
                            "tradeMode": "trend_following",
                            "allowRunner": True,
                            "trendline": structural_line.public(),
                            "liquidityTarget": liquidity_target.public() if liquidity_target else None,
                            "targetSource": "liquidity" if liquidity_target else "risk_multiple",
                        },
                    )
            lows = swing_lows(window)
            if len(lows) < 2:
                return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных минимумов"])
            projected, visuals = trend_line_visual(window, lows[-2], lows[-1])
            distance = abs(close - projected) / close
            if distance <= 0.0025 and bullish_rejection(candles[-1]):
                stop = min(candles[-1].low, projected) * 0.999
                risk = close - stop
                liquidity_target = find_liquidity_target(candles, close, Action.LONG, structure=structure)
                target = (
                    liquidity_target.price
                    if liquidity_target is not None
                    else close + risk * 1.6
                )
                return StrategyDecision(
                    strategy=self.key,
                    action=Action.LONG,
                    reasons=["Восходящая структура", "Откат к трендовой опоре", "Есть реакция покупателя"],
                    confidence=0.76,
                    watched_level=projected,
                    entry=close,
                    stop=stop,
                    target=target,
                    visuals=visuals,
                    details={
                        "setupQuality": 0.76,
                        "tradeMode": "trend_following",
                        "allowRunner": True,
                        "liquidityTarget": (
                            liquidity_target.public() if liquidity_target else None
                        ),
                        "targetSource": (
                            "liquidity" if liquidity_target else "risk_multiple"
                        ),
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

        structural_line = structure.trendline("resistance") if structure else None
        if structural_line is not None:
            projected = structural_line.current_price
            visuals = {
                "overlays": [{
                    "type": "line",
                    "label": "trend resistance",
                    "points": [
                        {"time": structural_line.start_ms // 1000, "price": structural_line.start_price},
                        {"time": structural_line.end_ms // 1000, "price": structural_line.end_price},
                    ],
                }]
            }
            distance = abs(close - projected) / close
            if distance <= 0.0025 and bearish_rejection(candles[-1]):
                stop = max(candles[-1].high, projected) * 1.001
                risk = stop - close
                liquidity_target = find_liquidity_target(
                    candles, close, Action.SHORT, structure=structure
                )
                target = liquidity_target.price if liquidity_target else close - risk * 1.6
                quality = min(0.92, 0.65 + structural_line.score * 0.25)
                return StrategyDecision(
                    strategy=self.key,
                    action=Action.SHORT,
                    reasons=[
                        "Нисходящая структура",
                        f"Касание наклонного сопротивления ({structural_line.touches} опор)",
                        "Есть реакция продавца",
                    ],
                    confidence=quality,
                    watched_level=projected,
                    entry=close,
                    stop=stop,
                    target=target,
                    visuals=visuals,
                    details={
                        "setupQuality": quality,
                        "tradeMode": "trend_following",
                        "allowRunner": True,
                        "trendline": structural_line.public(),
                        "liquidityTarget": liquidity_target.public() if liquidity_target else None,
                        "targetSource": "liquidity" if liquidity_target else "risk_multiple",
                    },
                )
        highs = swing_highs(window)
        if len(highs) < 2:
            return StrategyDecision(self.key, Action.WAIT, ["Недостаточно опорных максимумов"])
        projected, visuals = trend_line_visual(window, highs[-2], highs[-1])
        distance = abs(close - projected) / close
        if distance <= 0.0025 and bearish_rejection(candles[-1]):
            stop = max(candles[-1].high, projected) * 1.001
            risk = stop - close
            liquidity_target = find_liquidity_target(candles, close, Action.SHORT, structure=structure)
            target = (
                liquidity_target.price
                if liquidity_target is not None
                else close - risk * 1.6
            )
            return StrategyDecision(
                strategy=self.key,
                action=Action.SHORT,
                reasons=["Нисходящая структура", "Откат к трендовой опоре", "Есть реакция продавца"],
                confidence=0.76,
                watched_level=projected,
                entry=close,
                stop=stop,
                target=target,
                visuals=visuals,
                details={
                    "setupQuality": 0.76,
                    "tradeMode": "trend_following",
                    "allowRunner": True,
                    "liquidityTarget": (
                        liquidity_target.public() if liquidity_target else None
                    ),
                    "targetSource": (
                        "liquidity" if liquidity_target else "risk_multiple"
                    ),
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
