from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .structure import MarketStructure
from .common import (
    LevelKind,
    LevelZone,
    clamp,
    compute_trade_flow,
    detect_level_zones,
    typical_range_abs,
    typical_range_pct,
    zone_visual,
)
from .flow import flow_at_level, flow_beyond_level
from .liquidity import find_liquidity_targets
from .playbook_context import (
    PlaybookKind,
    assess_entry_context,
    breakout_direction_plan,
    position_context_supported,
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
    zone_key: tuple[str, str, float] | None = None
    stage: BreakoutStage = BreakoutStage.SEARCH
    used_generations: set[tuple[str, str, float]] = field(default_factory=set)
    break_started_at: float = 0.0


class LevelBreakoutStrategy(Strategy):
    key = "level_breakout"
    label = "Пробой наторгованного уровня"

    min_zone_touches = 5
    min_distinct_approaches = 4
    approach_pct = 0.0045
    max_stop_pct = 0.006
    max_zone_distance_pct = 0.012
    min_pressure_score = 3
    min_break_hold_seconds = 3.0
    minimum_target_r = 1.25

    def __init__(self) -> None:
        self._states: dict[str, BreakoutWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    @staticmethod
    def _select_breakout_target(
        entry: float,
        risk: float,
        action: Action,
        expected_impulse: float,
        liquidity_ladder: list,
        minimum_target_r: float,
    ) -> tuple[float, object | None, object | None, float]:
        nearest_obstacle = (
            liquidity_ladder[0]
            if liquidity_ladder
            else None
        )
        minimum_distance = risk * minimum_target_r
        liquidity_target = next(
            (
                row
                for row in liquidity_ladder
                if abs(row.price - entry) >= minimum_distance
            ),
            None,
        )
        fallback_distance = max(
            expected_impulse,
            minimum_distance,
        )
        fallback_target = (
            entry + fallback_distance
            if action == Action.LONG
            else entry - fallback_distance
        )
        target = (
            liquidity_target.price
            if liquidity_target is not None
            else fallback_target
        )
        target_r = (
            abs(target - entry) / risk
            if risk > 0
            else 0.0
        )
        return (
            target,
            nearest_obstacle,
            liquidity_target,
            target_r,
        )

    def mark_opened(self, symbol: str, decision: StrategyDecision) -> None:
        state = self._states.get(symbol)
        generation = decision.details.get("zoneGeneration")
        if state is not None and generation is not None:
            state.used_generations.add(tuple(generation))

    @staticmethod
    def _generation(zone: LevelZone) -> tuple[str, str, float]:
        return (
            zone.kind,
            str(zone.last_touch_index),
            round(zone.center, 8),
        )

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
                1
                for left, right in zip(recent[-4:-1], recent[-3:])
                if right.low >= left.low
            )
            flow_aligned = (
                flow["participationConfirmed"]
                and flow["imbalance5s"] >= 0.08
            )
        else:
            near_count = sum(1 for candle in recent if candle.close <= zone.high * 1.003)
            structure = sum(
                1
                for left, right in zip(recent[-4:-1], recent[-3:])
                if right.high <= left.high
            )
            flow_aligned = (
                flow["participationConfirmed"]
                and flow["imbalance5s"] <= -0.08
            )

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

    @staticmethod
    def _mature(zone: LevelZone, candles: list[Candle]) -> bool:
        local_range = typical_range_pct(candles)
        return (
            zone.touches >= LevelBreakoutStrategy.min_zone_touches
            and zone.reaction_pct >= local_range * 0.45
            and zone.volume_ratio >= 0.80
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
        market_context: "MarketContext | None" = None,
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
            back_inside = (
                side == Side.LONG
                and high > 0
                and executable < high
            ) or (
                side == Side.SHORT
                and low > 0
                and executable > low
            )
            key = "_breakoutBackInsideSinceMs"
            if back_inside:
                now_ms = (
                    observed_at_ms
                    if observed_at_ms is not None
                    else int(opened_at * 1000)
                )
                since = strategy_details.get(key)
                if since is None:
                    strategy_details[key] = now_ms
                elif (
                    now_ms - int(since)
                    >= int(self.min_break_hold_seconds * 1000)
                ):
                    return "breakout_failed_back_inside"
            else:
                strategy_details.pop(key, None)
        if unrealized_pnl >= 0:
            return None
        if not position_context_supported(
            PlaybookKind.LEVEL_BREAKOUT,
            side,
            market_context,
            trend,
        ):
            return "breakout_context_lost"
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
    ) -> StrategyDecision:
        context_plan = breakout_direction_plan(
            market_context,
            trend,
        )
        if (
            len(candles) < 60
            or not symbol
            or not context_plan.allowed_directions
        ):
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["MarketContext пока не даёт направление для breakout playbook"],
                details={
                    "state": BreakoutStage.SEARCH.value,
                    "playbookContext": context_plan.public(),
                    "legacyTrend": trend.value,
                },
            )

        state = self._states.setdefault(symbol, BreakoutWatchState())
        trades = trades or []
        price = book.mid or candles[-1].close

        candidates: list[
            tuple[float, Trend, LevelZone, list]
        ] = []
        for direction in context_plan.allowed_directions:
            long_candidate = direction == Trend.UP
            zone_kind: LevelKind = (
                "resistance"
                if long_candidate
                else "support"
            )
            if structure is not None:
                structural_rows = [
                    level
                    for level in structure.levels
                    if level.kind == zone_kind
                    and level.touches >= self.min_zone_touches
                    and level.distinct_approaches >= self.min_distinct_approaches
                    and level.reaction_pct
                    >= typical_range_pct(candles) * 0.45
                    and level.volume_ratio >= 0.80
                    and level.lifecycle == "worked"
                ]
                zones = [
                    level.as_zone()
                    for level in structural_rows
                ]
            else:
                structural_rows = []
                zones = detect_level_zones(
                    candles,
                    zone_kind,
                    min_touches=self.min_zone_touches,
                )
                zones = [
                    zone
                    for zone in zones
                    if self._mature(zone, candles)
                ]
            candidate_zone = self._select_zone(
                zones,
                price,
                long_side=long_candidate,
            )
            if candidate_zone is not None:
                candidates.append(
                    (
                        abs(candidate_zone.center - price),
                        direction,
                        candidate_zone,
                        structural_rows,
                    )
                )

        if not candidates:
            state.stage = BreakoutStage.SEARCH
            state.zone_key = None
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Зрелая наторгованная зона для разрешённого breakout-направления рядом не найдена"],
                details={
                    "state": state.stage.value,
                    "playbookContext": context_plan.public(),
                    "legacyTrend": trend.value,
                },
            )

        _, playbook_trend, zone, structural = min(
            candidates,
            key=lambda row: row[0],
        )
        long_side = playbook_trend == Trend.UP
        context_details = {
            "playbookContext": context_plan.public(),
            "playbookTrend": playbook_trend.value,
            "legacyTrend": trend.value,
        }

        generation = self._generation(zone)
        if structure is not None:
            matched = next(
                (
                    level
                    for level in structural
                    if abs(level.center - zone.center)
                    <= max(zone.width, price * 0.0006)
                ),
                None,
            )
            if matched is not None and matched.generation_id:
                generation = (
                    zone.kind,
                    matched.generation_id,
                    round(zone.center, 8),
                )
        if state.zone_key != generation:
            state.zone_key = generation
            state.stage = BreakoutStage.FOUND
            state.break_started_at = 0.0
        visuals = zone_visual(zone, "breakout zone")
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
        pressure_score, pressure = self._pressure_score(
            candles,
            zone,
            flow,
            long_side=long_side,
        )

        if generation in state.used_generations:
            state.stage = BreakoutStage.FOUND
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Эта генерация уровня уже была пробита и использована; ждём новую структуру"],
                0.35,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "alreadyUsed": True,
                },
            )

        if long_side:
            approach_distance = max(0.0, zone.low - price) / price
        else:
            approach_distance = max(0.0, price - zone.high) / price

        if approach_distance > self.approach_pct:
            state.stage = BreakoutStage.FOUND
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Зрелая зона найдена, цена ещё не подошла"],
                0.45,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                },
            )

        state.stage = (
            BreakoutStage.PRESSURE
            if pressure_score >= self.min_pressure_score
            else BreakoutStage.APPROACH
        )
        break_buffer = max(0.00015, book.spread_pct * 1.5)
        broke = (
            price > zone.high * (1 + break_buffer)
            if long_side
            else price < zone.low * (1 - break_buffer)
        )

        if not broke:
            state.break_started_at = 0.0
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    (
                        "Цена у зрелого уровня, давление на пробой сформировано"
                        if state.stage == BreakoutStage.PRESSURE
                        else "Цена у зрелого уровня, но давления пока недостаточно"
                    )
                ],
                min(0.48 + pressure_score * 0.05, 0.73),
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                },
            )

        state.stage = BreakoutStage.BREAK
        acceptance_boundary = zone.high if long_side else zone.low
        acceptance_flow = flow_beyond_level(
            trades,
            acceptance_boundary,
            long_side=long_side,
            seconds=5,
            now_ms=observed_at_ms,
        )
        aligned_after_break = (
            flow["participationConfirmed"]
            and acceptance_flow.trade_count >= 3
            and (
                acceptance_flow.imbalance >= 0.05
                if long_side
                else acceptance_flow.imbalance <= -0.05
            )
            and (
                acceptance_flow.price_response_pct >= -0.0001
                if long_side
                else acceptance_flow.price_response_pct <= 0.0001
            )
        )
        if pressure_score < self.min_pressure_score or not aligned_after_break:
            state.break_started_at = 0.0
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Зона проколота, но давление/поток недостаточны для подтверждённого пробоя"],
                0.55,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "acceptanceFlow": acceptance_flow.public(),
                    "acceptanceBoundary": acceptance_boundary,
                },
            )

        market_now = (
            observed_at_ms / 1000
            if observed_at_ms is not None
            else trades[-1].ts_ms / 1000
        )
        if state.break_started_at <= 0:
            state.break_started_at = market_now
        held_seconds = max(
            0.0,
            market_now - state.break_started_at,
        )
        if held_seconds < self.min_break_hold_seconds:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Пробой подтверждён потоком; ждём удержание цены за уровнем перед входом"],
                0.64,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "acceptanceFlow": acceptance_flow.public(),
                    "acceptanceBoundary": acceptance_boundary,
                    "breakHoldSeconds": held_seconds,
                    "requiredBreakHoldSeconds": self.min_break_hold_seconds,
                },
            )

        range_abs = typical_range_abs(candles)
        entry = price
        invalidation_buffer = max(
            zone.width * 0.45,
            range_abs * 0.20,
            entry * max(book.spread_pct * 2.0, 0.00025),
        )
        if long_side:
            stop = zone.high - invalidation_buffer
            stop_pct = (entry - stop) / entry
            action = Action.LONG
        else:
            stop = zone.low + invalidation_buffer
            stop_pct = (stop - entry) / entry
            action = Action.SHORT

        structural_risk = abs(entry - stop)
        expected_impulse = max(
            zone.width * 1.3,
            range_abs * 2.0,
        )
        liquidity_ladder = find_liquidity_targets(
            candles,
            entry,
            action,
            min_distance_pct=0.0,
            max_distance_pct=0.06,
            structure=structure,
        )
        (
            target,
            nearest_obstacle,
            liquidity_target,
            target_r,
        ) = self._select_breakout_target(
            entry,
            structural_risk,
            action,
            expected_impulse,
            liquidity_ladder,
            self.minimum_target_r,
        )

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
                    "zoneGeneration": generation,
                    "stopDistancePct": stop_pct,
                },
            )

        touch_quality = clamp(
            (zone.touches - self.min_zone_touches + 1) / 4.0
        )
        pressure_quality = clamp(pressure_score / 5.0)
        flow_quality = clamp(abs(level_flow.imbalance) / 0.25)
        reaction_quality = clamp(
            zone.reaction_pct
            / max(typical_range_pct(candles), 1e-9)
            / 2.0
        )
        approach_quality = (
            clamp(matched.distinct_approaches / 6.0)
            if structure is not None and matched is not None
            else touch_quality
        )
        dwell_quality = (
            clamp(matched.dwell_bars / 10.0)
            if structure is not None and matched is not None
            else 0.5
        )
        failed_break_quality = (
            clamp((matched.failed_breaks + matched.sweeps) / 4.0)
            if structure is not None and matched is not None
            else 0.0
        )
        structural_quality = (
            matched.score
            if structure is not None and matched is not None
            else reaction_quality
        )
        quality = clamp(
            0.25
            + touch_quality * 0.12
            + approach_quality * 0.14
            + dwell_quality * 0.08
            + failed_break_quality * 0.06
            + structural_quality * 0.10
            + pressure_quality * 0.15
            + flow_quality * 0.10
            + reaction_quality * 0.08
        )

        state.stage = BreakoutStage.IMPULSE
        setup_id = (
            f"{self.key}:{action.value}:{generation[0]}:"
            f"{generation[1]}:{generation[2]:.10g}"
        )
        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                "Пробой зрелой наторгованной горизонтальной зоны",
                f"Зона подтверждена {zone.touches} касаниями, реакциями и объёмом",
                "Подход сформировал давление, поток непосредственно у уровня подтверждает пробой",
                "Одна генерация уровня торгуется только один раз",
            ],
            confidence=quality,
            watched_level=zone.center,
            entry=entry,
            stop=stop,
            target=target,
            visuals=visuals,
            details={
                "state": state.stage.value,
                "zone": zone.public(),
                "zoneGeneration": generation,
                "levelLifecycle": (
                    matched.public()
                    if structure is not None and matched is not None
                    else None
                ),
                "flow": flow,
                "levelFlow": level_flow.public(),
                "acceptanceFlow": acceptance_flow.public(),
                "acceptanceBoundary": acceptance_boundary,
                "pressure": pressure,
                "pressureScore": pressure_score,
                "breakHoldSeconds": max(
                    0.0,
                    market_now - state.break_started_at,
                ),
                "requiredBreakHoldSeconds": self.min_break_hold_seconds,
                "expectedImpulsePct": (
                    expected_impulse / entry
                    if entry > 0
                    else None
                ),
                "stopDistancePct": stop_pct,
                "stopSource": "breakout_reacceptance_buffer",
                "invalidationBuffer": invalidation_buffer,
                "exitMode": "impulse_first",
                "nearestObstacle": (
                    nearest_obstacle.public() if nearest_obstacle else None
                ),
                "liquidityLadder": [
                    row.public() for row in liquidity_ladder[:8]
                ],
                "liquidityTarget": (
                    liquidity_target.public() if liquidity_target else None
                ),
                "minimumTargetR": self.minimum_target_r,
                "targetRiskMultipleGross": target_r,
                "targetSource": (
                    "liquidity_ladder"
                    if liquidity_target
                    else "impulse_risk_fallback"
                ),
                "tradeMode": "trend_following",
                "allowRunner": True,
                "setupQuality": quality,
                "qualityFactors": {
                    "touchMaturity": touch_quality,
                    "distinctApproaches": approach_quality,
                    "dwell": dwell_quality,
                    "failedBreakHistory": failed_break_quality,
                    "structuralQuality": structural_quality,
                    "pressure": pressure_quality,
                    "flow": flow_quality,
                    "reactionHistory": reaction_quality,
                },
            },
            setup_id=setup_id,
        )
