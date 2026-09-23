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
    ARMED = "armed"
    BREAK = "break"
    IMPULSE = "impulse"


@dataclass(slots=True)
class BreakoutWatchState:
    zone_key: tuple[str, str, float] | None = None
    stage: BreakoutStage = BreakoutStage.SEARCH
    used_generations: set[tuple[str, str, float]] = field(default_factory=set)
    armed_at: float = 0.0
    armed_until: float = 0.0
    armed_price: float = 0.0
    armed_zone: LevelZone | None = None
    armed_trend: Trend = Trend.FLAT
    armed_generation_id: str | None = None
    break_started_at: float = 0.0
    break_extreme: float = 0.0
    retest_seen: bool = False
    retest_at: float = 0.0
    probe_opened: bool = False


class LevelBreakoutStrategy(Strategy):
    key = "level_breakout"
    label = "Пробой наторгованного уровня"

    min_zone_touches = 5
    min_distinct_approaches = 4
    approach_pct = 0.0045
    max_stop_pct = 0.006
    max_zone_distance_pct = 0.012
    min_pressure_score = 3
    pressure_hysteresis_seconds = 10.0
    pressure_hysteresis_score_margin = 1
    min_break_hold_seconds = 3.0
    # With taker entry + maker exit, 1.25R cannot produce a 1:1 net payoff
    # for ordinary scalp stops. Require room for roughly 2R before costs.
    minimum_target_r = 2.0
    # Stage 18: raw breakouts are observation states, not entries. Production
    # keeps breakout scale-in off until retest/hold shows positive edge.
    staged_entries_enabled = False
    probe_risk_fraction = 0.35
    retest_tolerance_bps = 3.0
    hold_without_retest_seconds = 8.0
    absorption_efficiency_threshold = 0.35
    min_directional_response_bps = 5.0

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
        if state is None or generation is None:
            return
        staged = (
            decision.details.get("stagedEntry")
            if isinstance(
                decision.details.get("stagedEntry"),
                dict,
            )
            else {}
        )
        if staged.get("phase") == "probe":
            state.probe_opened = True
            state.stage = BreakoutStage.BREAK
            return
        state.used_generations.add(tuple(generation))
        state.probe_opened = False
        state.armed_zone = None
        state.armed_trend = Trend.FLAT
        state.armed_generation_id = None

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
    def _select_structural_candidate(
        levels: list["StructuralLevel"],
        price: float,
        *,
        long_side: bool,
    ) -> tuple[LevelZone, "StructuralLevel"] | None:
        pairs = [
            (level.as_zone(), level)
            for level in levels
        ]
        if long_side:
            eligible = [
                pair
                for pair in pairs
                if pair[0].high >= price * 0.997
                and pair[0].low
                <= price
                * (
                    1
                    + LevelBreakoutStrategy.max_zone_distance_pct
                )
            ]
        else:
            eligible = [
                pair
                for pair in pairs
                if pair[0].low <= price * 1.003
                and pair[0].high
                >= price
                * (
                    1
                    - LevelBreakoutStrategy.max_zone_distance_pct
                )
            ]
        eligible.sort(
            key=lambda pair: (
                abs(pair[0].center - price),
                -pair[0].score,
            )
        )
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
        market_now = (
            observed_at_ms / 1000
            if observed_at_ms is not None
            else (
                trades[-1].ts_ms / 1000
                if trades
                else candles[-1].start_ms / 1000 + 60.0
            )
        )
        pinned_arm = (
            state.armed_zone is not None
            and state.armed_trend in context_plan.allowed_directions
            and (
                state.probe_opened
                or (
                    state.armed_at > 0
                    and market_now <= state.armed_until
                )
            )
        )

        candidates: list[
            tuple[
                float,
                Trend,
                LevelZone,
                "StructuralLevel | None",
            ]
        ] = []
        if not pinned_arm:
            for direction in context_plan.allowed_directions:
                long_candidate = direction == Trend.UP
                zone_kind: LevelKind = (
                    "resistance"
                    if long_candidate
                    else "support"
                )
                matched_level = None
                if structure is not None:
                    structural_rows = [
                        level
                        for level in structure.levels
                        if level.kind == zone_kind
                        and level.touches >= self.min_zone_touches
                        and level.distinct_approaches
                        >= self.min_distinct_approaches
                        and level.reaction_pct
                        >= typical_range_pct(candles) * 0.45
                        and level.volume_ratio >= 0.80
                        and level.lifecycle == "worked"
                    ]
                    selected = self._select_structural_candidate(
                        structural_rows,
                        price,
                        long_side=long_candidate,
                    )
                    candidate_zone = (
                        selected[0]
                        if selected is not None
                        else None
                    )
                    matched_level = (
                        selected[1]
                        if selected is not None
                        else None
                    )
                else:
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
                            matched_level,
                        )
                    )

        matched = None
        if pinned_arm:
            playbook_trend = state.armed_trend
            zone = state.armed_zone
            if (
                structure is not None
                and state.armed_generation_id is not None
            ):
                matched = next(
                    (
                        level
                        for level in structure.levels
                        if level.generation_id
                        == state.armed_generation_id
                    ),
                    None,
                )
                if matched is None:
                    state.stage = BreakoutStage.SEARCH
                    state.zone_key = None
                    state.armed_zone = None
                    state.armed_trend = Trend.FLAT
                    state.armed_generation_id = None
                    return StrategyDecision(
                        self.key,
                        Action.WAIT,
                        [
                            "Prepared breakout level generation "
                            "disappeared; hypothesis reset"
                        ],
                        details={
                            "state": state.stage.value,
                            "preparedGenerationId": (
                                state.armed_generation_id
                            ),
                        },
                    )
        else:
            if not candidates:
                state.stage = BreakoutStage.SEARCH
                state.zone_key = None
                state.armed_zone = None
                state.armed_trend = Trend.FLAT
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

            _, playbook_trend, zone, matched = min(
                candidates,
                key=lambda row: row[0],
            )
        if zone is None:
            state.stage = BreakoutStage.SEARCH
            state.zone_key = None
            state.armed_zone = None
            state.armed_trend = Trend.FLAT
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Prepared breakout hypothesis lost its market object"],
                details={"state": state.stage.value},
            )
        long_side = playbook_trend == Trend.UP
        context_details = {
            "playbookContext": context_plan.public(),
            "playbookTrend": playbook_trend.value,
            "legacyTrend": trend.value,
        }

        generation = self._generation(zone)
        if matched is not None and matched.generation_id:
            generation = (
                zone.kind,
                matched.generation_id,
                round(zone.center, 8),
            )
        if state.zone_key != generation:
            state.zone_key = generation
            state.stage = BreakoutStage.FOUND
            state.armed_at = 0.0
            state.armed_until = 0.0
            state.armed_price = 0.0
            state.armed_zone = None
            state.armed_trend = Trend.FLAT
            state.armed_generation_id = None
            state.break_started_at = 0.0
            state.break_extreme = 0.0
            state.retest_seen = False
            state.retest_at = 0.0
            state.probe_opened = False
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
        forming = (
            market_context.forming_candle
            if market_context is not None
            else None
        )
        forming_pressure = False
        if forming is not None and forming.age_seconds >= 0.5:
            directional_body = (
                forming.body_pct > 0
                if long_side
                else forming.body_pct < 0
            )
            terminal_close = (
                forming.close_position >= 0.65
                if long_side
                else forming.close_position <= 0.35
            )
            directional_velocity = (
                forming.velocity_bps_per_second > 0
                if long_side
                else forming.velocity_bps_per_second < 0
            )
            live_expansion = (
                (
                    forming.volume_pace_ratio is not None
                    and forming.volume_pace_ratio >= 1.0
                )
                or (
                    forming.range_expansion_ratio is not None
                    and forming.range_expansion_ratio >= 0.90
                )
            )
            forming_pressure = (
                directional_body
                and terminal_close
                and directional_velocity
                and forming.body_to_range >= 0.40
                and live_expansion
            )
            if forming_pressure:
                # Live 1m evidence may contribute one point, but cannot arm
                # a breakout on its own. Confirmed structure + flow remain
                # the majority of the pressure score.
                pressure_score += 1

        pressure = {
            **pressure,
            "formingPressure": forming_pressure,
            "formingCandle": (
                forming.public()
                if forming is not None
                else None
            ),
        }

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
                    **context_details,
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
                    **context_details,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                },
            )

        pressure_ready = pressure_score >= self.min_pressure_score
        arm_active = (
            state.armed_at > 0
            and market_now <= state.armed_until
        )
        hysteresis_floor = max(
            0,
            self.min_pressure_score
            - self.pressure_hysteresis_score_margin,
        )
        if pressure_ready:
            if not arm_active:
                state.armed_at = market_now
                state.armed_price = price
            state.armed_zone = zone
            state.armed_trend = playbook_trend
            state.armed_generation_id = (
                matched.generation_id
                if matched is not None
                else None
            )
            state.armed_until = max(
                state.armed_until,
                market_now + self.pressure_hysteresis_seconds,
            )
            state.stage = BreakoutStage.ARMED
            arm_active = True
        elif arm_active and pressure_score >= hysteresis_floor:
            state.stage = BreakoutStage.ARMED
        else:
            state.stage = BreakoutStage.APPROACH
            if not state.probe_opened:
                state.armed_zone = None
                state.armed_trend = Trend.FLAT
                state.armed_generation_id = None

        opportunity_arm = (
            {
                "observedAtMs": int(state.armed_at * 1000),
                "price": state.armed_price,
                "source": "breakout_pressure_armed",
            }
            if state.armed_at > 0 and arm_active
            else None
        )
        prepared_opportunity = (
            {
                "preparedAtMs": int(state.armed_at * 1000),
                "source": "breakout_pressure_armed",
                "action": (
                    Action.LONG.value
                    if long_side
                    else Action.SHORT.value
                ),
                "generation": list(generation),
                "structuralGenerationId": (
                    matched.generation_id
                    if matched is not None
                    else None
                ),
                "watchedLevel": zone.center,
                "armPrice": state.armed_price,
                "pinned": True,
            }
            if (
                state.armed_at > 0
                and (arm_active or state.probe_opened)
            )
            else None
        )
        break_buffer = max(0.00015, book.spread_pct * 1.5)
        broke = (
            price > zone.high * (1 + break_buffer)
            if long_side
            else price < zone.low * (1 - break_buffer)
        )

        if not broke:
            state.break_started_at = 0.0
            state.break_extreme = 0.0
            state.retest_seen = False
            state.retest_at = 0.0
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    (
                        "Breakout hypothesis ARMED: давление сохранено с hysteresis"
                        if state.stage == BreakoutStage.ARMED
                        else "Цена у зрелого уровня, но давления пока недостаточно"
                    )
                ],
                min(0.48 + pressure_score * 0.05, 0.73),
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    **context_details,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "pressureHysteresisActive": arm_active,
                    "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
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
        pressure_supported = (
            pressure_ready
            or (
                arm_active
                and pressure_score >= hysteresis_floor
            )
        )
        local_break_flow = (
            level_flow.trade_count >= 3
            and (
                level_flow.imbalance >= 0.02
                if long_side
                else level_flow.imbalance <= -0.02
            )
        )
        directional_response_bps = (
            (
                level_flow.price_response_pct
                if long_side
                else -level_flow.price_response_pct
            )
            * 10_000
        )
        breakout_absorbed = (
            level_flow.trade_count >= 3
            and abs(level_flow.imbalance) >= 0.20
            and level_flow.absorption_efficiency
            >= self.absorption_efficiency_threshold
            and directional_response_bps
            < self.min_directional_response_bps
        )
        probe_flow_supported = (
            local_break_flow
            and not breakout_absorbed
            and (
                aligned_after_break
                or (
                    arm_active
                    and forming_pressure
                )
            )
        )
        if not pressure_supported or not probe_flow_supported:
            if not arm_active and not state.probe_opened:
                state.break_started_at = 0.0
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    (
                        "Breakout probe уже открыт; ждём подтверждение "
                        "acceptance/hold для add"
                        if state.probe_opened
                        else (
                            "Агрессивный поток поглощается без достаточного "
                            "движения цены; breakout вход запрещён"
                            if breakout_absorbed
                            else "Зона проколота, но давление/pre-state/flow "
                            "недостаточны для подтверждения breakout"
                        )
                    )
                ],
                0.55,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    **context_details,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "acceptanceFlow": acceptance_flow.public(),
                    "acceptanceBoundary": acceptance_boundary,
                    "pressureHysteresisActive": arm_active,
                    "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
                    "probeOpened": state.probe_opened,
                    "localBreakFlowConfirmed": local_break_flow,
                    "breakoutAbsorbed": breakout_absorbed,
                    "directionalResponseBps": directional_response_bps,
                    "absorptionEfficiency": (
                        level_flow.absorption_efficiency
                    ),
                },
            )

        if state.break_started_at <= 0:
            state.break_started_at = market_now
            state.break_extreme = price
            state.retest_seen = False
            state.retest_at = 0.0
        if long_side:
            state.break_extreme = max(
                state.break_extreme or price,
                price,
            )
            excursion_bps = max(
                0.0,
                (state.break_extreme - zone.high)
                / max(zone.high, 1e-9)
                * 10_000,
            )
            near_boundary = (
                price
                <= zone.high
                * (1 + self.retest_tolerance_bps / 10_000)
            )
        else:
            if state.break_extreme <= 0:
                state.break_extreme = price
            state.break_extreme = min(
                state.break_extreme,
                price,
            )
            excursion_bps = max(
                0.0,
                (zone.low - state.break_extreme)
                / max(zone.low, 1e-9)
                * 10_000,
            )
            near_boundary = (
                price
                >= zone.low
                * (1 - self.retest_tolerance_bps / 10_000)
            )

        meaningful_excursion = (
            excursion_bps
            >= self.retest_tolerance_bps * 2.0
        )
        if (
            meaningful_excursion
            and near_boundary
            and not state.retest_seen
        ):
            state.retest_seen = True
            state.retest_at = market_now

        held_seconds = max(
            0.0,
            market_now - state.break_started_at,
        )
        retest_hold_seconds = (
            max(0.0, market_now - state.retest_at)
            if state.retest_seen and state.retest_at > 0
            else 0.0
        )
        retest_hold_ready = (
            state.retest_seen
            and retest_hold_seconds
            >= self.min_break_hold_seconds
        )
        sustained_response_ready = (
            directional_response_bps
            >= self.min_directional_response_bps
        )
        sustained_hold_ready = (
            held_seconds
            >= self.hold_without_retest_seconds
            and sustained_response_ready
        )
        confirmation_mode = (
            "retest_hold"
            if retest_hold_ready
            else (
                "sustained_price_response"
                if sustained_hold_ready
                else None
            )
        )
        confirmation_ready = (
            aligned_after_break
            and not breakout_absorbed
            and confirmation_mode is not None
        )

        if self.staged_entries_enabled:
            probe_fraction = max(
                0.05,
                min(float(self.probe_risk_fraction), 0.80),
            )
            if state.probe_opened and not confirmation_ready:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    [
                        "Breakout probe в позиции; ждём acceptance + hold "
                        "перед использованием оставшегося risk budget"
                    ],
                    0.64,
                    zone.center,
                    visuals=visuals,
                    details={
                        "state": state.stage.value,
                        **context_details,
                        "zone": zone.public(),
                        "zoneGeneration": generation,
                        "pressureScore": pressure_score,
                        "pressure": pressure,
                        "flow": flow,
                        "levelFlow": level_flow.public(),
                        "acceptanceFlow": acceptance_flow.public(),
                        "acceptanceBoundary": acceptance_boundary,
                        "breakHoldSeconds": held_seconds,
                        "requiredBreakHoldSeconds": (
                            self.min_break_hold_seconds
                        ),
                        "probeOpened": True,
                        "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
                    },
                )
            if state.probe_opened:
                staged_phase = "add"
                staged_risk_fraction = 1.0 - probe_fraction
            elif confirmation_ready:
                staged_phase = "full"
                staged_risk_fraction = 1.0
            else:
                staged_phase = "probe"
                staged_risk_fraction = probe_fraction
        else:
            if not confirmation_ready:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    [
                        "BREAK наблюдается; ждём retest+hold либо устойчивое "
                        "acceptance за уровнем перед FIRE"
                    ],
                    0.64,
                    zone.center,
                    visuals=visuals,
                    details={
                        "state": state.stage.value,
                        **context_details,
                        "zone": zone.public(),
                        "zoneGeneration": generation,
                        "pressureScore": pressure_score,
                        "pressure": pressure,
                        "flow": flow,
                        "levelFlow": level_flow.public(),
                        "acceptanceFlow": acceptance_flow.public(),
                        "acceptanceBoundary": acceptance_boundary,
                        "breakHoldSeconds": held_seconds,
                        "requiredBreakHoldSeconds": (
                            self.min_break_hold_seconds
                        ),
                        "retestSeen": state.retest_seen,
                        "retestHoldSeconds": retest_hold_seconds,
                        "sustainedHoldSecondsRequired": (
                            self.hold_without_retest_seconds
                        ),
                        "sustainedResponseReady": (
                            sustained_response_ready
                        ),
                        "breakoutConfirmationMode": (
                            confirmation_mode
                        ),
                        "breakoutAbsorbed": breakout_absorbed,
                        "directionalResponseBps": (
                            directional_response_bps
                        ),
                    },
                )
            staged_phase = "full"
            staged_risk_fraction = 1.0

        range_abs = typical_range_abs(candles)
        entry = price
        invalidation_buffer = max(
            zone.width * 0.45,
            range_abs * 0.20,
            entry * max(book.spread_pct * 2.0, 0.00025),
        )
        if long_side:
            # Soft reacceptance inside the zone is managed separately by
            # manage_position(). The hard stop belongs beyond the opposite
            # edge of the complete breakout zone.
            stop = zone.low - invalidation_buffer
            stop_pct = (entry - stop) / entry
            action = Action.LONG
        else:
            stop = zone.high + invalidation_buffer
            stop_pct = (stop - entry) / entry
            action = Action.SHORT

        entry_context = assess_entry_context(
            PlaybookKind.LEVEL_BREAKOUT,
            action,
            market_context,
            trend,
        )
        if not entry_context.allowed:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    "Пробой подтверждён, но MarketContext блокирует breakout-вход",
                    *entry_context.blockers,
                ],
                0.55,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    **context_details,
                    "zone": zone.public(),
                    "zoneGeneration": generation,
                    "pressureScore": pressure_score,
                    "pressure": pressure,
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "acceptanceFlow": acceptance_flow.public(),
                    "acceptanceBoundary": acceptance_boundary,
                    "breakHoldSeconds": held_seconds,
                    "entryContextAssessment": entry_context.public(),
                },
            )

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
                    **context_details,
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

        state.stage = (
            BreakoutStage.BREAK
            if staged_phase == "probe"
            else BreakoutStage.IMPULSE
        )
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
                (
                    "Ранний probe разрешён сильным ARMED/pre-state; "
                    "остаток риска ждёт acceptance + hold"
                    if staged_phase == "probe"
                    else (
                        "Probe подтверждён acceptance + hold; добавляем "
                        "только зарезервированный остаток риска"
                        if staged_phase == "add"
                        else "Пробой полностью подтверждён acceptance + hold"
                    )
                ),
                "Одна генерация уровня торгуется только один раз после завершения staged entry",
            ],
            confidence=quality,
            watched_level=zone.center,
            entry=entry,
            stop=stop,
            target=target,
            visuals=visuals,
            details={
                "state": state.stage.value,
                **context_details,
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
                "entryContextAssessment": entry_context.public(),
                "acceptanceBoundary": acceptance_boundary,
                "pressure": pressure,
                "pressureHysteresisActive": arm_active,
                "opportunityArm": opportunity_arm,
                "preparedOpportunity": prepared_opportunity,
                "fireTrigger": {
                    "observedAtMs": (
                        observed_at_ms
                        if observed_at_ms is not None
                        else int(market_now * 1000)
                    ),
                    "source": (
                        "breakout_early_probe"
                        if staged_phase == "probe"
                        else (
                            "breakout_retest_hold"
                            if confirmation_mode == "retest_hold"
                            else "breakout_sustained_price_response"
                        )
                    ),
                    "preparedAtMs": (
                        int(state.armed_at * 1000)
                        if state.armed_at > 0
                        else None
                    ),
                },
                "pressureScore": pressure_score,
                "stagedEntry": {
                    "phase": staged_phase,
                    "riskFraction": staged_risk_fraction,
                    "probeRiskFraction": (
                        max(
                            0.05,
                            min(
                                float(self.probe_risk_fraction),
                                0.80,
                            ),
                        )
                        if self.staged_entries_enabled
                        else 0.0
                    ),
                    "confirmationReady": confirmation_ready,
                    "probeOpened": state.probe_opened,
                },
                "breakHoldSeconds": max(
                    0.0,
                    market_now - state.break_started_at,
                ),
                "requiredBreakHoldSeconds": self.min_break_hold_seconds,
                "retestSeen": state.retest_seen,
                "retestHoldSeconds": retest_hold_seconds,
                "sustainedHoldSecondsRequired": (
                    self.hold_without_retest_seconds
                ),
                "sustainedResponseReady": (
                    sustained_response_ready
                ),
                "breakoutConfirmationMode": (
                    confirmation_mode
                ),
                "breakoutAbsorbed": breakout_absorbed,
                "directionalResponseBps": directional_response_bps,
                "expectedImpulsePct": (
                    expected_impulse / entry
                    if entry > 0
                    else None
                ),
                "stopDistancePct": stop_pct,
                "stopSource": "hard_beyond_breakout_zone",
                "softInvalidation": "sustained_reacceptance_inside_zone",
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
                "tradeMode": "breakout",
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
