from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from enum import StrEnum

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
    from .structure import MarketStructure
from .common import (
    LevelKind,
    LevelZone,
    approach_is_directional,
    clamp,
    compute_trade_flow,
    detect_level_zones,
    nearby_round_level,
    typical_range_abs,
    zone_overlap_count,
    zone_visual,
)
from .flow import flow_at_level, flow_beyond_level
from .liquidity import find_liquidity_targets


class RejectionStage(StrEnum):
    SEARCH = "search"
    FOUND = "found"
    APPROACH = "approach"
    TEST = "test"
    REJECT = "reject"
    REACTION = "reaction"


@dataclass(slots=True)
class RejectionWatchState:
    zone_key: str | tuple[str, float, float, int] | None = None
    stage: RejectionStage = RejectionStage.SEARCH
    used_generations: set[str] = field(default_factory=set)
    pinned_zone: LevelZone | None = None
    pinned_generation_id: str | None = None
    pinned_until: float = 0.0
    swept: bool = False


class WeakLevelRejectionStrategy(Strategy):
    key = "weak_level_rejection"
    label = "Отбой от слабого уровня"

    max_touches = 3
    approach_pct = 0.005
    max_stop_pct = 0.006
    test_pin_seconds = 150.0
    test_pin_max_distance_pct = 0.008

    def __init__(self) -> None:
        self._states: dict[str, RejectionWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def mark_opened(self, symbol: str, decision: StrategyDecision) -> None:
        state = self._states.get(symbol)
        generation = decision.details.get("levelGeneration")
        if state is not None and generation:
            state.used_generations.add(str(generation))
            state.pinned_zone = None
            state.pinned_generation_id = None
            state.pinned_until = 0.0
            state.swept = False

    @staticmethod
    def _key(zone: LevelZone) -> tuple[str, float, float, int]:
        return (
            zone.kind,
            round(zone.low, 10),
            round(zone.high, 10),
            zone.last_touch_index,
        )

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
            if zone_overlap_count(candles[:-1], zone, lookback=8) > 3:
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

    def _decision_for_zone(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        trades: list[TradeTick],
        symbol: str,
        zone: LevelZone,
        structure: "MarketStructure | None" = None,
        structural_level=None,
        observed_at_ms: int | None = None,
    ) -> StrategyDecision:
        state = self._states.setdefault(symbol, RejectionWatchState())
        generation_id = (
            structural_level.generation_id
            if structural_level is not None and structural_level.generation_id
            else f"{zone.kind}:{zone.last_touch_index}:{zone.center:.10g}"
        )
        zone_key = generation_id
        if state.zone_key != zone_key:
            state.zone_key = zone_key
            state.stage = RejectionStage.FOUND

        if generation_id in state.used_generations:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Эта генерация слабого уровня уже использована; ждём новый уровень"],
                0.30,
                zone.center,
                details={
                    "state": RejectionStage.FOUND.value,
                    "zone": zone.public(),
                    "levelGeneration": generation_id,
                    "alreadyUsed": True,
                },
            )

        last = candles[-1]
        price = book.mid or last.close
        flow = compute_trade_flow(trades, observed_at_ms)
        level_tolerance = max(
            zone.width_pct * 1.5,
            book.spread_pct * 2.0,
            0.0015,
        )
        level_flow = flow_at_level(
            trades,
            zone.center,
            tolerance_pct=level_tolerance,
            seconds=15,
            now_ms=observed_at_ms,
        )
        recent_level_flow = flow_at_level(
            trades,
            zone.center,
            tolerance_pct=level_tolerance,
            seconds=5,
            now_ms=observed_at_ms,
        )
        visuals = zone_visual(zone, "weak rejection zone")
        range_abs = typical_range_abs(candles)

        if not approach_is_directional(candles, zone.kind):
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
                    "levelFlow": level_flow.public(),
                    "weakLevel": True,
                },
            )

        state.stage = RejectionStage.APPROACH
        round_level = nearby_round_level(zone.center, max(range_abs * 0.35, zone.width))
        buffer = max(range_abs * 0.20, price * 0.00015)

        if zone.kind == "resistance":
            tested = last.high >= zone.low
            breakout_flow = flow_beyond_level(
                trades,
                zone.high,
                long_side=True,
                seconds=15,
                now_ms=observed_at_ms,
            )
            failed_break = (
                last.high > zone.high
                and last.close < zone.low
                and breakout_flow.trade_count > 0
            )
            attack_absorbed = (
                level_flow.buy_notional > level_flow.sell_notional
                and level_flow.absorption_efficiency >= 0.30
            )
            flow_reversed = (
                flow["participationConfirmed"]
                and recent_level_flow.trade_count >= 3
                and recent_level_flow.imbalance <= -0.03
            )
            action = Action.SHORT
            stop_anchor = max(zone.high, round_level or zone.high)
            stop = stop_anchor + buffer
            risk = stop - price
        else:
            tested = last.low <= zone.high
            breakout_flow = flow_beyond_level(
                trades,
                zone.low,
                long_side=False,
                seconds=15,
                now_ms=observed_at_ms,
            )
            failed_break = (
                last.low < zone.low
                and last.close > zone.high
                and breakout_flow.trade_count > 0
            )
            attack_absorbed = (
                level_flow.sell_notional > level_flow.buy_notional
                and level_flow.absorption_efficiency >= 0.30
            )
            flow_reversed = (
                flow["participationConfirmed"]
                and recent_level_flow.trade_count >= 3
                and recent_level_flow.imbalance >= 0.03
            )
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
                    "levelFlow": level_flow.public(),
                    "recentLevelFlow": recent_level_flow.public(),
                    "breakoutFlow": breakout_flow.public(),
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
                    "levelFlow": level_flow.public(),
                    "recentLevelFlow": recent_level_flow.public(),
                    "breakoutFlow": breakout_flow.public(),
                    "roundLevel": round_level,
                    "weakLevel": True,
                },
            )

        expected_action = Action.LONG if trend == Trend.UP else Action.SHORT
        if action != expected_action:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Отбой подтверждён, но направлен против HTF тренда; контртрендовый вход запрещён"],
                0.48,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "roundLevel": round_level,
                    "weakLevel": True,
                    "levelGeneration": generation_id,
                    "trendAligned": False,
                    "rejectedAction": action.value,
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

        mode = "trend_following"
        allow_runner = True
        target_r = 1.6
        reaction_target = (
            price + risk * target_r
            if action == Action.LONG
            else price - risk * target_r
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
        liquidity_target = next(
            (
                row
                for row in liquidity_ladder
                if abs(row.price - price) >= risk * target_r
            ),
            None,
        )
        target = (
            liquidity_target.price
            if liquidity_target is not None
            else reaction_target
        )

        approaches = (
            structural_level.distinct_approaches
            if structural_level is not None
            else zone.touches
        )
        acceptance_bars = (
            structural_level.acceptance_bars
            if structural_level is not None
            else 0
        )
        failed_breaks = (
            structural_level.failed_breaks
            if structural_level is not None
            else 0
        )
        freshness = clamp(1.0 - max(0, approaches - 1) * 0.30)
        clean_acceptance = clamp(1.0 - acceptance_bars / 4.0)
        rejection_history = clamp(failed_breaks / 2.0)
        flow_strength = clamp(abs(level_flow.imbalance) / 0.25)
        absorption_quality = clamp(level_flow.absorption_efficiency / 0.50)
        quality = clamp(
            0.42
            + freshness * 0.16
            + clean_acceptance * 0.10
            + rejection_history * 0.08
            + flow_strength * 0.12
            + absorption_quality * 0.06
            + (0.06 if allow_runner else 0.0)
            + (0.03 if round_level is not None else 0.0)
        )
        state.stage = RejectionStage.REACTION

        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                f"Слабый уровень: {approaches} отдельных подход(а), без длительной проторговки",
                "Попытка пробоя не удержалась, цена вернулась за границу зоны",
                "Поток непосредственно у уровня подтвердил разворот/поглощение",
                "Отскок подтверждён в направлении HTF тренда",
            ],
            confidence=quality,
            watched_level=zone.center,
            entry=price,
            stop=stop,
            target=target,
            visuals=visuals,
            details={
                "state": state.stage.value,
                "zone": zone.public(),
                "flow": flow,
                "levelFlow": level_flow.public(),
                "recentLevelFlow": recent_level_flow.public(),
                "breakoutFlow": breakout_flow.public(),
                "roundLevel": round_level,
                "weakLevel": True,
                "levelGeneration": generation_id,
                "levelLifecycle": (
                    structural_level.public()
                    if structural_level is not None
                    else None
                ),
                "tradeMode": mode,
                "allowRunner": allow_runner,
                "exitMode": "runner_allowed",
                "targetR": target_r,
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
                    liquidity_target.public() if liquidity_target else None
                ),
                "targetSource": (
                    "liquidity_ladder"
                    if liquidity_target is not None
                    else "risk_multiple"
                ),
                "setupQuality": quality,
                "qualityFactors": {
                    "freshness": freshness,
                    "cleanAcceptance": clean_acceptance,
                    "rejectionHistory": rejection_history,
                    "flowStrength": flow_strength,
                    "absorptionAtLevel": absorption_quality,
                    "roundConfluence": round_level is not None,
                    "trendAligned": allow_runner,
                },
            },
        )

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
        observed_at_ms: int | None = None,
    ) -> str | None:
        zone = (
            strategy_details.get("zone")
            if isinstance(strategy_details, dict)
            else None
        )
        if isinstance(zone, dict):
            low = float(zone.get("low") or 0)
            high = float(zone.get("high") or 0)
            executable = (
                book.executable_exit(side)
                if book is not None
                else None
            ) or last_price
            invalid = (
                side == Side.LONG
                and low > 0
                and executable < low
            ) or (
                side == Side.SHORT
                and high > 0
                and executable > high
            )
            key = "_weakLevelInvalidSinceMs"
            if invalid:
                now_ms = (
                    observed_at_ms
                    if observed_at_ms is not None
                    else int(opened_at * 1000)
                )
                since = strategy_details.get(key)
                if since is None:
                    strategy_details[key] = now_ms
                elif now_ms - int(since) >= 3_000:
                    return "weak_level_invalidated"
            else:
                strategy_details.pop(key, None)
        if unrealized_pnl >= 0:
            return None
        mode = str(strategy_details.get("tradeMode") or "")
        expected = Trend.UP if side == Side.LONG else Trend.DOWN
        if mode == "trend_following" and trend != expected:
            return "weak_level_context_lost"
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

        resistance_level = support_level = None
        if structure is not None:
            resistance_level = structure.nearest_horizontal(
                price,
                "resistance",
                max_distance_pct=self.approach_pct,
                min_touches=1,
                max_touches=self.max_touches,
            )
            support_level = structure.nearest_horizontal(
                price,
                "support",
                max_distance_pct=self.approach_pct,
                min_touches=1,
                max_touches=self.max_touches,
            )

            def young(level):
                return (
                    level is not None
                    and 1 <= level.distinct_approaches <= 3
                    and level.acceptance_bars <= 3
                    and level.lifecycle in {"fresh", "tested"}
                    and level.generation_id is not None
                )

            resistance = (
                resistance_level.as_zone()
                if young(resistance_level)
                else None
            )
            support = (
                support_level.as_zone()
                if young(support_level)
                else None
            )
        else:
            resistance = self._select_weak_zone(candles, price, "resistance")
            support = self._select_weak_zone(candles, price, "support")
        choices = [zone for zone in (resistance, support) if zone is not None]
        if not choices:
            state = self._states.setdefault(symbol, RejectionWatchState())
            state.stage = RejectionStage.SEARCH
            state.zone_key = None
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Рядом нет молодого слабонаторгованного уровня"],
                details={"state": RejectionStage.SEARCH.value},
            )

        zone = min(choices, key=lambda item: abs(item.center - price))
        structural_level = None
        if structure is not None:
            candidates = [
                level
                for level in (resistance_level, support_level)
                if level is not None
                and abs(level.center - zone.center)
                <= max(zone.width, price * 0.0006)
            ]
            structural_level = (
                min(candidates, key=lambda item: abs(item.center - price))
                if candidates
                else None
            )
        return self._decision_for_zone(
            candles,
            book,
            trend,
            trades or [],
            symbol,
            zone,
            structure,
            structural_level,
            observed_at_ms=observed_at_ms,
        )
