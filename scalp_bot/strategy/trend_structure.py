from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median
from typing import TYPE_CHECKING

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy
from .scenario_objects import trendline_object_id, matches_assignment
from .targets import structural_target, movement_budget
from .common import compute_trade_flow
from .flow import flow_at_level
from .liquidity import find_liquidity_targets
from .playbook_context import (
    PlaybookKind,
    assess_entry_context,
    continuation_direction_plan,
    position_context_supported,
)

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .structure import MarketStructure, TrendLine


class TrendPullbackStage(StrEnum):
    SEARCH = "search"
    PULLBACK = "pullback"
    ARMED = "armed"
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
    armed_at: float = 0.0
    armed_price: float = 0.0
    reclaim_at: float = 0.0
    used_anchors: set[tuple] = field(default_factory=set)


class TrendStructureStrategy(Strategy):
    key = "trend_structure"
    label = "Трендовый откат с подтверждением"

    approach_pct = 0.004
    test_tolerance_pct = 0.0025
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
    def _prepared_opportunity(
        state: TrendPullbackState,
        *,
        long_side: bool,
        anchor: tuple,
    ) -> dict | None:
        if state.armed_at <= 0:
            return None
        return {
            "preparedAtMs": int(state.armed_at * 1000),
            "source": "trendline_live_test_armed",
            "action": (
                Action.LONG.value
                if long_side
                else Action.SHORT.value
            ),
            "anchor": list(anchor),
            "watchedLevel": state.test_line_price,
            "armPrice": state.armed_price,
            "reclaimLevel": state.reclaim_level,
            "invalidationExtreme": state.test_extreme,
            "pinned": True,
        }

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
    def _micro_reclaim_level(
        cls,
        projected: float,
        book: OrderBook,
        *,
        long_side: bool,
    ) -> float:
        confirmation_pct = max(
            max(book.spread_pct, 0.0) * 2.0,
            cls.test_tolerance_pct * 0.20,
        )
        return (
            projected * (1 + confirmation_pct)
            if long_side
            else projected * (1 - confirmation_pct)
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
        trade_flow: dict | None = None,
    ) -> tuple[bool, dict, dict]:
        flow = compute_trade_flow(trades, now_ms) if trade_flow is None else trade_flow
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

        effort_without_result = bool(
            flow.get("effortWithoutResult5s")
        )
        flow["trendContinuationEffortWithoutResult"] = (
            effort_without_result
        )
        return (
            recent_aligned
            and level_confirmed
            and not effort_without_result
        ), flow, local.public()

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
        book: OrderBook | None = None,
        market_context: "MarketContext | None" = None,
        observed_at_ms: int | None = None,
    ) -> str | None:
        if (
            not strategy_details.get("scenario")
            and unrealized_pnl < 0
            and not position_context_supported(
                PlaybookKind.TREND_CONTINUATION,
                side,
                market_context,
                trend,
            )
        ):
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
        market_context: "MarketContext | None" = None,
        observed_at_ms: int | None = None,
        trade_flow: dict | None = None,
    ) -> StrategyDecision:
        state = self._states.setdefault(symbol, TrendPullbackState())
        trades = trades or []
        context_plan = continuation_direction_plan(
            market_context,
            trend,
        )
        playbook_trend = context_plan.primary_direction

        if (
            len(candles) < 40
            or playbook_trend == Trend.FLAT
            or structure is None
        ):
            if state.trend != playbook_trend:
                state.stage = TrendPullbackStage.SEARCH
                state.anchor_key = None
                state.used_anchors.clear()
            state.trend = playbook_trend
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Локальный MarketContext пока не даёт трендовый continuation playbook"],
                details={
                    "state": state.stage.value,
                    "playbookContext": context_plan.public(),
                    "legacyTrend": trend.value,
                },
            )

        long_side = playbook_trend == Trend.UP
        kind = "support" if long_side else "resistance"
        eligible_lines = [line for line in structure.trendlines if line.kind == kind
            and matches_assignment(market_context, self.key, trendline_object_id(line))]
        line = max(eligible_lines, key=lambda line: line.score) if eligible_lines else None
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
            state.trend = playbook_trend
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Тренд есть, но нет подтверждённой линии правильного наклона минимум с 3 опорами"],
                details={"state": state.stage.value},
            )

        anchor = self._anchor_key(line)
        if state.trend != playbook_trend:
            state = TrendPullbackState(trend=playbook_trend)
            self._states[symbol] = state
        if state.anchor_key != anchor:
            state.stage = TrendPullbackStage.SEARCH
            state.anchor_key = anchor
            state.test_line_price = 0.0
            state.test_extreme = 0.0
            state.reclaim_level = 0.0
            state.reclaim_price = 0.0
            state.armed_at = 0.0
            state.armed_price = 0.0
            state.reclaim_at = 0.0
        state.trend = playbook_trend

        visuals = self._visuals(line)
        price = book.mid or candles[-1].close
        projected = line.current_price
        distance = abs(price - projected) / price if price > 0 else 999.0
        forming = (
            market_context.forming_candle
            if market_context is not None
            else None
        )
        directional_closed = self._pullback_is_directional(
            candles,
            long_side=long_side,
        )
        forming_pullback = (
            forming is not None
            and (
                (
                    long_side
                    and forming.body_pct < 0
                    and forming.close_position <= 0.55
                )
                or (
                    not long_side
                    and forming.body_pct > 0
                    and forming.close_position >= 0.45
                )
            )
        )
        directional = directional_closed or forming_pullback
        pullback_character = self._pullback_character(
            candles,
            directional=directional_closed,
        )

        common_details = {
            "state": state.stage.value,
            "tradeMode": "trend_following",
            "allowRunner": True,
            "trendline": line.public(),
            "trendlineAnchor": anchor,
            "currentPrice": price,
            "projectedTrendlinePrice": projected,
            "distancePct": distance,
            "approachPct": self.approach_pct,
            "testTolerancePct": self.test_tolerance_pct,
            "deepBreakPct": self.deep_break_pct,
            "pullbackDirectional": directional,
            "closedPullbackDirectional": directional_closed,
            "formingPullbackDirectional": forming_pullback,
            "formingCandle": (
                forming.public()
                if forming is not None
                else None
            ),
            "pullbackCharacter": pullback_character,
            "playbookContext": context_plan.public(),
            "playbookTrend": playbook_trend.value,
            "legacyTrend": trend.value,
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
            state.stage in {
                TrendPullbackStage.SEARCH,
                TrendPullbackStage.PULLBACK,
            }
            and pullback_character["aggressiveCountertrend"]
        ):
            state.stage = TrendPullbackStage.SEARCH
            state.armed_at = 0.0
            state.armed_price = 0.0
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
        live_high = forming.high if forming is not None else last.high
        live_low = forming.low if forming is not None else last.low
        live_close = forming.close if forming is not None else last.close
        test_price = live_low if long_side else live_high
        test_distance = (
            abs(test_price - projected) / price
            if price > 0
            else 999.0
        )
        deep_penetration_pct = (
            max(0.0, (projected - test_price) / projected)
            if long_side and projected > 0
            else (
                max(0.0, (test_price - projected) / projected)
                if projected > 0
                else 0.0
            )
        )
        close_penetration_pct = (
            max(0.0, (projected - live_close) / projected)
            if long_side and projected > 0
            else (
                max(0.0, (live_close - projected) / projected)
                if projected > 0
                else 0.0
            )
        )
        confirmed_close_penetration_pct = (
            max(0.0, (projected - last.close) / projected)
            if long_side and projected > 0
            else (
                max(0.0, (last.close - projected) / projected)
                if projected > 0
                else 0.0
            )
        )
        swept_trendline = deep_penetration_pct > self.deep_break_pct
        # Forming candles may sweep and recover.  Only a confirmed close can
        # permanently invalidate the trendline setup.
        accepted_break = (
            confirmed_close_penetration_pct > self.deep_break_pct
        )

        if accepted_break:
            state.stage = TrendPullbackStage.SEARCH
            state.armed_at = 0.0
            state.armed_price = 0.0
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Подтверждённая свеча закрылась слишком глубоко за трендовой опорой; setup сброшен"],
                0.30,
                projected,
                visuals=visuals,
                details={
                    **common_details,
                    "deepBreak": True,
                    "acceptedBreak": True,
                    "sweptTrendline": swept_trendline,
                    "testPrice": test_price,
                    "testDistancePct": test_distance,
                    "deepPenetrationPct": deep_penetration_pct,
                    "closePenetrationPct": close_penetration_pct,
                    "confirmedClosePenetrationPct": (
                        confirmed_close_penetration_pct
                    ),
                    "lastClosedPrice": last.close,
                    "liveClosePrice": live_close,
                },
            )

        if state.stage == TrendPullbackStage.PULLBACK:
            if (
                test_distance > self.test_tolerance_pct
                and not swept_trendline
            ):
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Откат идёт к подтверждённой опоре; live pre-state ещё не дал реальный тест"],
                    0.50,
                    projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "testDistancePct": test_distance,
                        "sweptTrendline": swept_trendline,
                        "deepPenetrationPct": deep_penetration_pct,
                        "closePenetrationPct": close_penetration_pct,
                        "confirmedClosePenetrationPct": (
                            confirmed_close_penetration_pct
                        ),
                    },
                )

            state.stage = TrendPullbackStage.ARMED
            state.test_line_price = projected
            state.test_extreme = test_price
            state.reclaim_level = self._micro_reclaim_level(
                projected,
                book,
                long_side=long_side,
            )
            state.armed_at = (
                observed_at_ms / 1000
                if observed_at_ms is not None
                else last.start_ms / 1000 + 60.0
            )
            state.armed_price = price
            reclaim_distance_bps = (
                abs(state.reclaim_level - projected)
                / projected
                * 10_000
                if projected > 0
                else 0.0
            )
            opportunity_arm = {
                "observedAtMs": int(state.armed_at * 1000),
                "price": state.armed_price,
                "source": "trendline_live_test_armed",
            }
            prepared_opportunity = self._prepared_opportunity(
                state,
                long_side=long_side,
                anchor=anchor,
            )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Trend hypothesis ARMED по формирующейся 1m свече; ждём micro reclaim и качественный price response"],
                0.60,
                projected,
                visuals=visuals,
                details={
                    **common_details,
                    "state": state.stage.value,
                    "reclaimLevel": state.reclaim_level,
                    "reclaimDistanceBps": reclaim_distance_bps,
                    "testExtreme": state.test_extreme,
                    "sweptTrendline": swept_trendline,
                    "deepPenetrationPct": deep_penetration_pct,
                    "closePenetrationPct": close_penetration_pct,
                    "confirmedClosePenetrationPct": (
                        confirmed_close_penetration_pct
                    ),
                    "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
                },
            )

        flow_ok, flow, level_flow = self._flow_confirmation(
            trades=trades,
            book=book,
            level=state.test_line_price or projected,
            long_side=long_side,
            now_ms=observed_at_ms,
            trade_flow=trade_flow,
        )

        opportunity_arm = (
            {
                "observedAtMs": int(state.armed_at * 1000),
                "price": state.armed_price,
                "source": "trendline_live_test_armed",
            }
            if state.armed_at > 0
            else None
        )
        prepared_opportunity = self._prepared_opportunity(
            state,
            long_side=long_side,
            anchor=anchor,
        )
        forming_contradictory = (
            forming is not None
            and forming.age_seconds >= 1.0
            and (
                (
                    long_side
                    and forming.velocity_bps_per_second < 0
                    and forming.close_position < 0.40
                )
                or (
                    not long_side
                    and forming.velocity_bps_per_second > 0
                    and forming.close_position > 0.60
                )
            )
        )

        if state.stage in {
            TrendPullbackStage.ARMED,
            TrendPullbackStage.TEST,
        }:
            state.stage = TrendPullbackStage.TEST
            reclaimed = (
                price > state.reclaim_level
                if long_side
                else price < state.reclaim_level
            )
            response_ready = (
                reclaimed
                and flow_ok
                and not forming_contradictory
            )
            if not response_ready:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    [
                        "Live test ARMED; ждём micro reclaim с price response, "
                        "не похожим на exhaustion"
                    ],
                    0.62,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "reclaimLevel": state.reclaim_level,
                        "reclaimed": reclaimed,
                        "flowConfirmed": flow_ok,
                        "formingContradictory": forming_contradictory,
                        "effortWithoutResult": bool(
                            flow.get(
                                "trendContinuationEffortWithoutResult"
                            )
                        ),
                        "opportunityArm": opportunity_arm,
                        "preparedOpportunity": prepared_opportunity,
                        "flow": flow,
                        "levelFlow": level_flow,
                    },
                )
            state.stage = TrendPullbackStage.RECLAIM
            state.reclaim_price = price
            state.reclaim_at = (
                observed_at_ms / 1000
                if observed_at_ms is not None
                else state.armed_at
            )
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Micro reclaim подтверждён; проверяем удержание и качество response вместо chase за дополнительным импульсом"],
                0.72,
                state.test_line_price or projected,
                visuals=visuals,
                details={
                    **common_details,
                    "state": state.stage.value,
                    "reclaimLevel": state.reclaim_level,
                    "reclaimPrice": state.reclaim_price,
                    "flowConfirmed": True,
                    "formingContradictory": False,
                    "opportunityArm": opportunity_arm,
                        "preparedOpportunity": prepared_opportunity,
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
                    ["Micro reclaim не удержан; возвращаемся к ARMED/TEST без нового chase"],
                    0.50,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "reclaimLost": True,
                        "opportunityArm": opportunity_arm,
                        "preparedOpportunity": prepared_opportunity,
                    },
                )

            response_confirmed = (
                flow_ok
                and not forming_contradictory
                and not bool(
                    flow.get(
                        "trendContinuationEffortWithoutResult"
                    )
                )
            )
            if not response_confirmed:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Reclaim удержан, но price response/flow пока не подтверждают ранний continuation"],
                    0.62,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "responseConfirmed": False,
                        "flowConfirmed": flow_ok,
                        "formingContradictory": forming_contradictory,
                        "effortWithoutResult": bool(
                            flow.get(
                                "trendContinuationEffortWithoutResult"
                            )
                        ),
                        "opportunityArm": opportunity_arm,
                        "preparedOpportunity": prepared_opportunity,
                        "flow": flow,
                        "levelFlow": level_flow,
                    },
                )

            action = Action.LONG if long_side else Action.SHORT
            entry_context = assess_entry_context(
                PlaybookKind.TREND_CONTINUATION,
                action,
                market_context,
                trend,
            )
            if not entry_context.allowed:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    [
                        "Continuation подтверждён локально, но MarketContext блокирует вход",
                        *entry_context.blockers,
                    ],
                    0.55,
                    state.test_line_price or projected,
                    visuals=visuals,
                    details={
                        **common_details,
                        "state": state.stage.value,
                        "responseConfirmed": response_confirmed,
                        "flowConfirmed": flow_ok,
                        "flow": flow,
                        "levelFlow": level_flow,
                        "entryContextAssessment": entry_context.public(),
                    },
                )

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

            liquidity_ladder = find_liquidity_targets(
                candles,
                price,
                action,
                min_distance_pct=0.0,
                structure=structure,
            )
            nearest_obstacle = (
                liquidity_ladder[0]
                if liquidity_ladder
                else None
            )
            target, liquidity_target, target_source = structural_target(
                price, action, liquidity_ladder, movement=movement_budget(candles))

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
                    "Локальный continuation-контекст подтверждён MarketContext",
                    "Состоялся направленный pullback к подтверждённой трендовой опоре",
                    "Цена вернула micro structure после теста",
                    "Trade flow подтвердил возврат инициативы по тренду",
                    "Reclaim удержался без effort-without-result / противоречащего pre-state",
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
                    "responseConfirmed": response_confirmed,
                    "formingContradictory": forming_contradictory,
                    "effortWithoutResult": bool(
                        flow.get(
                            "trendContinuationEffortWithoutResult"
                        )
                    ),
                    "opportunityArm": opportunity_arm,
                    "fireTrigger": {
                        "observedAtMs": observed_at_ms,
                        "source": "trend_reclaim_price_response",
                        "preparedAtMs": (
                            int(state.armed_at * 1000)
                            if state.armed_at > 0
                            else None
                        ),
                    },
                        "preparedOpportunity": prepared_opportunity,
                    "flow": flow,
                    "levelFlow": level_flow,
                    "entryContextAssessment": entry_context.public(),
                    "targetR": abs(target-price) / abs(price-stop),
                    "expectedImpulsePct": movement_budget(candles) / price,
                    "nearestObstacle": (
                        nearest_obstacle.public()
                        if nearest_obstacle
                        else None
                    ),
                    "liquidityLadder": [
                        row.public()
                        for row in liquidity_ladder[:8]
                    ],
                    "liquidityTarget": (
                        liquidity_target.public()
                        if liquidity_target
                        else None
                    ),
                    "targetSource": (
                        "liquidity_ladder"
                        if liquidity_target
                        else "observed_range_projection"
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
