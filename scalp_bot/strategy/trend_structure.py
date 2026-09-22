from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median
from typing import TYPE_CHECKING

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy
from .common import compute_trade_flow
from .flow import flow_at_level
from .liquidity import find_liquidity_target

if TYPE_CHECKING:
    from .structure import MarketStructure, TrendLine


class TrendPullbackStage(StrEnum):
    SEARCH = "search"
    PULLBACK = "pullback"
    TEST = "test"
    RECLAIM = "reclaim"
    CONTINUATION = "continuation"


@dataclass(slots=True)
class TrendPullbackState:
    stage: TrendPullbackStage = TrendPullbackStage.SEARCH
    trend: Trend = Trend.FLAT
    anchor_key: tuple | None = None
    test_line_price: float = 0.0
    test_extreme: float = 0.0
    reclaim_level: float = 0.0
    reclaim_price: float = 0.0
    used_anchors: set[tuple] = field(default_factory=set)


class TrendStructureStrategy(Strategy):
    key = "trend_structure"
    label = "Трендовый откат с подтверждением"

    approach_pct = 0.004
    test_tolerance_pct = 0.0015
    deep_break_pct = 0.0025
    continuation_bps = 1.0
    aggressive_pullback_volume_ratio = 1.35
    aggressive_pullback_range_ratio = 1.35

    def __init__(self) -> None:
        self._states: dict[str, TrendPullbackState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def mark_opened(self, symbol: str, decision: StrategyDecision) -> None:
        state = self._states.get(symbol)
        anchor = decision.details.get("trendlineAnchor")
        if state is not None and anchor is not None:
            state.used_anchors.add(tuple(anchor))
            state.stage = TrendPullbackStage.CONTINUATION

    @staticmethod
    def _anchor_key(line: "TrendLine") -> tuple:
        return (
            line.kind,
            line.timeframe,
            line.start_ms,
            round(line.start_price, 8),
            round(line.slope_per_bar, 10),
        )

    @staticmethod
    def _pullback_is_directional(
        candles: list[Candle],
        *,
        long_side: bool,
    ) -> bool:
        if len(candles) < 5:
            return False
        closes = [c.close for c in candles[-5:-1]]
        pairs = list(zip(closes, closes[1:]))
        if long_side:
            return (
                closes[-1] < closes[0]
                and sum(right <= left for left, right in pairs) >= 2
            )
        return (
            closes[-1] > closes[0]
            and sum(right >= left for left, right in pairs) >= 2
        )

    @classmethod
    def _pullback_character(
        cls,
        candles: list[Candle],
        *,
        directional: bool,
    ) -> dict:
        if len(candles) < 15:
            return {
                "volumeRatio": 1.0,
                "rangeRatio": 1.0,
                "aggressiveCountertrend": False,
            }
        pullback = candles[-5:-1]
        baseline = candles[-15:-5]
        baseline_volumes = [c.volume for c in baseline if c.volume > 0]
        baseline_ranges = [
            c.high - c.low
            for c in baseline
            if c.high >= c.low
        ]
        if not pullback or not baseline_volumes or not baseline_ranges:
            return {
                "volumeRatio": 1.0,
                "rangeRatio": 1.0,
                "aggressiveCountertrend": False,
            }
        pullback_volume = sum(max(c.volume, 0.0) for c in pullback) / len(pullback)
        pullback_range = sum(max(0.0, c.high - c.low) for c in pullback) / len(pullback)
        volume_ratio = pullback_volume / max(median(baseline_volumes), 1e-9)
        range_ratio = pullback_range / max(median(baseline_ranges), 1e-9)
        aggressive = (
            directional
            and volume_ratio >= cls.aggressive_pullback_volume_ratio
            and range_ratio >= cls.aggressive_pullback_range_ratio
        )
        return {
            "volumeRatio": volume_ratio,
            "rangeRatio": range_ratio,
            "aggressiveCountertrend": aggressive,
        }

    def _flow_confirmation(
        self,
        *,
        trades: list[TradeTick],
        book: OrderBook,
        level: float,
        long_side: bool,
        now_ms: int | None = None,
    ) -> tuple[bool, dict, dict]:
        flow = compute_trade_flow(trades, now_ms)
        tolerance = max(
            self.test_tolerance_pct,
            book.spread_pct * 2.0,
        )
        local = flow_at_level(
            trades,
            level,
            tolerance_pct=tolerance,
            seconds=15,
            now_ms=now_ms,
        )

        if long_side:
            recent_aligned = (
                flow["participationConfirmed"]
                and flow["tradeCount5s"] >= 3
                and flow["imbalance5s"] >= 0.03
                and flow["cvd5s"] > 0
            )
            attack_absorbed = (
                local.sell_notional > local.buy_notional
                and local.absorption_efficiency >= 0.25
                and local.price_response_pct >= 0
            )
            level_confirmed = (
                local.trade_count >= 3
                and (
                    (
                        local.imbalance >= 0.03
                        and local.price_response_pct >= -0.0001
                    )
                    or attack_absorbed
                )
            )
        else:
            recent_aligned = (
                flow["participationConfirmed"]
                and flow["tradeCount5s"] >= 3
                and flow["imbalance5s"] <= -0.03
                and flow["cvd5s"] < 0
            )
            attack_absorbed = (
                local.buy_notional > local.sell_notional
                and local.absorption_efficiency >= 0.25
                and local.price_response_pct <= 0
            )
            level_confirmed = (
                local.trade_count >= 3
                and (
                    (
                        local.imbalance <= -0.03
                        and local.price_response_pct <= 0.0001
                    )
                    or attack_absorbed
                )
            )

        return recent_aligned and level_confirmed, flow, local.public()

    @staticmethod
    def _visuals(line: "TrendLine") -> dict:
        return {
            "overlays": [{
                "type": "line",
                "label": "confirmed trend pullback line",
                "points": [
                    {
                        "time": line.start_ms // 1000,
                        "price": line.start_price,
                    },
                    {
                        "time": line.end_ms // 1000,
                        "price": line.end_price,
                    },
                ],
            }]
        }

    def manage_position(
        self,
        *,
        side: Side,
        unrealized_pnl: float,
        opened_at: float,
        strategy_details: dict,
        decision: StrategyDecision | None,
        trend: Trend,
        last_price: float,
    ) -> str | None:
        expected = Trend.UP if side == Side.LONG else Trend.DOWN
        if unrealized_pnl < 0 and trend != expected:
            return "trend_structure_context_lost"
        return None

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
        structure: "MarketStructure | None" = None,
        observed_at_ms: int | None = None,
    ) -> StrategyDecision:
        state = self._states.setdefault(symbol, TrendPullbackState())
        trades = trades or []

        if len(candles) < 40 or trend == Trend.FLAT or structure is None:
            if state.trend != trend:
                state.stage = TrendPullbackStage.SEARCH
                state.anchor_key = None
                state.used_anchors.clear()
            state.trend = trend
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Нет подтверждённого трендового контекста"],
                details={"state": state.stage.value},
            )

        long_side = trend == Trend.UP
        kind = "support" if long_side else "resistance"
        line = structure.trendline(kind)
        slope_aligned = (
            line is not None
            and (
                (long_side and line.slope_per_bar > 0)
                or (not long_side and line.slope_per_bar < 0)
            )
        )
        if line is None or line.touches < 3 or not slope_aligned:
            state.stage = TrendPullbackStage.SEARCH
            state.anchor_key = None
            state.trend = trend
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Тренд есть, но нет подтверждённой линии правильного наклона минимум с 3 опорами"],
                details={"state": state.stage.value},
            )

        anchor = self._anchor_key(line)
        if state.trend != trend:
            state = TrendPullbackState(trend=trend)
            self._states[symbol] = state
        if state.anchor_key != anchor:
            state.stage = TrendPullbackStage.SEARCH
            state.anchor_key = anchor
            state.test_line_price = 0.0
            state.test_extreme = 0.0
            state.reclaim_level = 0.0
            state.reclaim_price = 0.0
        state.trend = trend

        visuals = self._visuals(line)
        price = book.mid or candles[-1].close
        projected = line.current_price
        distance = abs(price - projected) / price if price > 0 else 999.0
        directional = self._pullback_is_directional(
            candles,
            long_side=long_side,
        )
        pullback_character = self._pullback_character(
            candles,
            directional=directional,
        )

        common_details = {
            "state": state.stage.value,
            "tradeMode": "trend_following",
            "allowRunner": True,
            "trendline": line.public(),
            "trendlineAnchor": anchor,
            "pullbackDirectional": directional,
            "pullbackCharacter": pullback_character,
        }

        if anchor in state.used_anchors:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Эта трендовая опора уже была использована; ждём новую структуру"],
                0.35,
                projected,
                visuals=visuals,
                details={**common_details, "alreadyUsed": True},
            )

        if (
            state.stage in {TrendPullbackStage.SEARCH, TrendPullbackStage.PULLBACK}
            and pullback_character["aggressiveCountertrend"]
        ):
            state.stage = TrendPullbackStage.SEARCH
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    "Встречное движение слишком активно по объёму и диапазону; "
                    "это больше похоже на импульс против тренда, чем на слабый pullback"
                ],
                0.30,
                projected,
                visuals=visuals,
                details={
                    **common_details,
                    "state": state.stage.value,
                    "aggressiveCountertrend": True,
                },
            )

        if state.stage == TrendPullbackStage.SEARCH:
            if not directional or distance > self.approach_pct:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Тренд подтверждён; ждём направленный откат к опоре"],
                    0.42,
                    projected,
                    visuals=visuals,
                    details=common_details,
                )
            state.stage = TrendPullbackStage.PULLBACK

        last = candles[-1]
        test_price = last.low if long_side else last.high
        test_distance = (
            abs(test_price - projected) / price
            if price > 0
            else 999.0
        )
        deeply_broken = (
            test_price < projected * (1 - self.deep_break_pct)
            if long_side
            else test_price > projected * (1 + self.deep_break_pct)
        )

        if deeply_broken:
            state.stage = TrendPullbackStage.SEARCH
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Откат пробил трендовую опору слишком глубоко; setup сброшен"],
                0.30,
                projected,
                visuals=visuals,
                details={**common_details, "deepBreak": True},
            )

        if state.stage == TrendPullbackStage.PULLBACK:
            if test_distance > self.test_tolerance_pct:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Откат идёт к подтверждённой опоре; ждём реальный тест"],
                    0.50,
                    projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "testDistancePct": test_distance,
                    },
                )

            state.stage = TrendPullbackStage.TEST
            state.test_line_price = projected
            state.test_extreme = test_price
            recent = candles[-3:]
            state.reclaim_level = (
                max(c.high for c in recent)
                if long_side
                else min(c.low for c in recent)
            )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Опора протестирована; вход запрещён до micro reclaim и подтверждения потока"],
                0.58,
                projected,
                visuals=visuals,
                details={
                    **common_details,
                    "state": state.stage.value,
                    "reclaimLevel": state.reclaim_level,
                    "testExtreme": state.test_extreme,
                },
            )

        flow_ok, flow, level_flow = self._flow_confirmation(
            trades=trades,
            book=book,
            level=state.test_line_price or projected,
            long_side=long_side,
            now_ms=observed_at_ms,
        )

        if state.stage == TrendPullbackStage.TEST:
            reclaimed = (
                price > state.reclaim_level
                if long_side
                else price < state.reclaim_level
            )
            if not (reclaimed and flow_ok):
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Тест состоялся; ждём micro reclaim и возврат инициативы по тренду"],
                    0.62,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "reclaimLevel": state.reclaim_level,
                        "reclaimed": reclaimed,
                        "flowConfirmed": flow_ok,
                        "flow": flow,
                        "levelFlow": level_flow,
                    },
                )
            state.stage = TrendPullbackStage.RECLAIM
            state.reclaim_price = price
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Micro reclaim подтверждён; ждём короткое продолжение импульса"],
                0.70,
                state.test_line_price or projected,
                visuals=visuals,
                details={
                    **common_details,
                    "state": state.stage.value,
                    "reclaimLevel": state.reclaim_level,
                    "reclaimPrice": state.reclaim_price,
                    "flowConfirmed": True,
                    "flow": flow,
                    "levelFlow": level_flow,
                },
            )

        if state.stage == TrendPullbackStage.RECLAIM:
            reclaim_lost = (
                price <= state.reclaim_level
                if long_side
                else price >= state.reclaim_level
            )
            if reclaim_lost:
                state.stage = TrendPullbackStage.TEST
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Micro reclaim не удержан; возвращаемся к ожиданию подтверждения"],
                    0.50,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "reclaimLost": True,
                    },
                )

            continuation_pct = max(
                self.continuation_bps / 10_000,
                book.spread_pct * 2.0,
            )
            continued = (
                price >= state.reclaim_price * (1 + continuation_pct)
                if long_side
                else price <= state.reclaim_price * (1 - continuation_pct)
            )
            if not (continued and flow_ok):
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Reclaim удержан; ждём продолжение и сохранение потока"],
                    0.68,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "continued": continued,
                        "flowConfirmed": flow_ok,
                        "flow": flow,
                        "levelFlow": level_flow,
                    },
                )

            action = Action.LONG if long_side else Action.SHORT
            buffer = max(price * 0.0008, abs(price - (state.test_line_price or projected)) * 0.10)
            if long_side:
                stop = min(
                    state.test_extreme,
                    state.test_line_price or projected,
                ) - buffer
                risk = price - stop
            else:
                stop = max(
                    state.test_extreme,
                    state.test_line_price or projected,
                ) + buffer
                risk = stop - price

            if risk <= 0:
                state.stage = TrendPullbackStage.SEARCH
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["После подтверждения нет корректной структурной invalidation"],
                    visuals=visuals,
                )

            liquidity_target = find_liquidity_target(
                candles,
                price,
                action,
                structure=structure,
            )
            target = (
                liquidity_target.price
                if liquidity_target is not None
                else (
                    price + risk * 1.6
                    if long_side
                    else price - risk * 1.6
                )
            )

            quality = min(
                0.94,
                0.62
                + line.score * 0.20
                + min(abs(flow["imbalance5s"]) / 0.30, 1.0) * 0.07
                + min(abs(level_flow["imbalance"]) / 0.30, 1.0) * 0.05,
            )
            setup_id = (
                f"{self.key}:{action.value}:{line.timeframe}:"
                f"{line.start_ms}:{state.reclaim_level:.10g}"
            )
            return StrategyDecision(
                strategy=self.key,
                action=action,
                reasons=[
                    "HTF тренд подтверждён",
                    "Состоялся направленный pullback к подтверждённой трендовой опоре",
                    "Цена вернула micro structure после теста",
                    "Trade flow подтвердил возврат инициативы по тренду",
                    "После reclaim появился follow-through",
                ],
                confidence=quality,
                watched_level=state.test_line_price or projected,
                entry=price,
                stop=stop,
                target=target,
                visuals=visuals,
                details={
                    **common_details,
                    "state": TrendPullbackStage.CONTINUATION.value,
                    "setupQuality": quality,
                    "reclaimLevel": state.reclaim_level,
                    "reclaimPrice": state.reclaim_price,
                    "flowConfirmed": True,
                    "flow": flow,
                    "levelFlow": level_flow,
                    "liquidityTarget": (
                        liquidity_target.public()
                        if liquidity_target
                        else None
                    ),
                    "targetSource": (
                        "liquidity"
                        if liquidity_target
                        else "risk_multiple"
                    ),
                },
                setup_id=setup_id,
            )

        return StrategyDecision(
            self.key,
            Action.WAIT,
            ["Трендовый setup уже использован; ждём новую структуру"],
            0.35,
            state.test_line_price or projected,
            visuals=visuals,
            details={**common_details, "state": state.stage.value},
        )
