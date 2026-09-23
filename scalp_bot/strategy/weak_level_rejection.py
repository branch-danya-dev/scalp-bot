from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from enum import StrEnum

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
    from .market_context import MarketContext
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
from .playbook_context import (
    PlaybookKind,
    assess_entry_context,
    position_context_supported,
    rejection_direction_plan,
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
    zone_key: str | tuple[str, float, float, int] | None = None
    stage: RejectionStage = RejectionStage.SEARCH
    used_generations: set[str] = field(default_factory=set)
    pinned_zone: LevelZone | None = None
    pinned_generation_id: str | None = None
    pinned_until: float = 0.0
    swept: bool = False
    armed_at: float = 0.0
    armed_price: float = 0.0
    probe_opened: bool = False


class WeakLevelRejectionStrategy(Strategy):
    key = "weak_level_rejection"
    label = "Отбой от слабого уровня"

    max_touches = 3
    approach_pct = 0.005
    max_stop_pct = 0.006
    test_pin_seconds = 150.0
    test_pin_max_distance_pct = 0.008
    staged_entries_enabled = False
    probe_risk_fraction = 0.30

    def __init__(self) -> None:
        self._states: dict[str, RejectionWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def mark_opened(self, symbol: str, decision: StrategyDecision) -> None:
        state = self._states.get(symbol)
        generation = decision.details.get("levelGeneration")
        if state is None or not generation:
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
            state.stage = RejectionStage.REJECT
            return
        state.used_generations.add(str(generation))
        state.probe_opened = False
        state.pinned_zone = None
        state.pinned_generation_id = None
        state.pinned_until = 0.0
        state.swept = False
        state.armed_at = 0.0
        state.armed_price = 0.0

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
        generation_id_override: str | None = None,
        market_context: "MarketContext | None" = None,
    ) -> StrategyDecision:
        state = self._states.setdefault(symbol, RejectionWatchState())
        context_plan = rejection_direction_plan(
            market_context,
            trend,
        )
        context_details = {
            "playbookContext": context_plan.public(),
            "legacyTrend": trend.value,
        }
        generation_id = (
            generation_id_override
            or (
                structural_level.generation_id
                if structural_level is not None and structural_level.generation_id
                else f"{zone.kind}:{zone.last_touch_index}:{zone.center:.10g}"
            )
        )
        now = (
            observed_at_ms / 1000
            if observed_at_ms is not None
            else (
                trades[-1].ts_ms / 1000
                if trades
                else candles[-1].start_ms / 1000
            )
        )
        zone_key = generation_id
        if state.zone_key != zone_key:
            state.zone_key = zone_key
            state.stage = RejectionStage.FOUND
            state.pinned_zone = None
            state.pinned_generation_id = None
            state.pinned_until = 0.0
            state.swept = False
            state.armed_at = 0.0
            state.armed_price = 0.0
            state.probe_opened = False

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
        forming = (
            market_context.forming_candle
            if market_context is not None
            else None
        )
        live_high = forming.high if forming is not None else last.high
        live_low = forming.low if forming is not None else last.low
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

        forming_approach = (
            forming is not None
            and (
                (
                    zone.kind == "resistance"
                    and forming.body_pct > 0
                    and forming.close_position >= 0.55
                )
                or (
                    zone.kind == "support"
                    and forming.body_pct < 0
                    and forming.close_position <= 0.45
                )
            )
        )
        if (
            not approach_is_directional(candles, zone.kind)
            and not forming_approach
        ):
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
            tested = live_high >= zone.low
            breakout_flow = flow_beyond_level(
                trades,
                zone.high,
                long_side=True,
                seconds=15,
                now_ms=observed_at_ms,
            )
            sweep_observed = (
                live_high > zone.high
                or breakout_flow.trade_count > 0
            )
            if sweep_observed:
                state.swept = True
            reclaimed = price < zone.low
            failed_break = state.swept and reclaimed
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
            tested = live_low <= zone.high
            breakout_flow = flow_beyond_level(
                trades,
                zone.low,
                long_side=False,
                seconds=15,
                now_ms=observed_at_ms,
            )
            sweep_observed = (
                live_low < zone.low
                or breakout_flow.trade_count > 0
            )
            if sweep_observed:
                state.swept = True
            reclaimed = price > zone.high
            failed_break = state.swept and reclaimed
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
            state.pinned_zone = zone
            state.pinned_generation_id = generation_id
            state.pinned_until = max(
                state.pinned_until,
                now + self.test_pin_seconds,
            )
            if state.armed_at <= 0:
                state.armed_at = now
                state.armed_price = price

        opportunity_arm = (
            {
                "observedAtMs": int(state.armed_at * 1000),
                "price": state.armed_price,
                "source": "rejection_live_test_armed",
            }
            if state.armed_at > 0
            else None
        )
        prepared_opportunity = (
            {
                "preparedAtMs": int(state.armed_at * 1000),
                "source": "rejection_live_test_armed",
                "action": action.value,
                "generation": str(generation_id),
                "watchedLevel": zone.center,
                "armPrice": state.armed_price,
                "zone": zone.public(),
                "pinned": True,
            }
            if state.armed_at > 0
            else None
        )

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
                    "sweepObserved": state.swept,
                    "reclaimed": failed_break,
                    "pinnedUntil": state.pinned_until or None,
                    "formingCandle": (
                        forming.public()
                        if forming is not None
                        else None
                    ),
                    "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
                },
            )

        state.stage = RejectionStage.REJECT
        early_absorption_ready = (
            attack_absorbed
            and not flow_reversed
        )
        probe_candidate = (
            self.staged_entries_enabled
            and not state.probe_opened
            and early_absorption_ready
        )
        late_reaction_only = (
            flow_reversed
            and not state.probe_opened
        )
        if (
            (not flow_reversed and not early_absorption_ready)
            or (
                not self.staged_entries_enabled
                and late_reaction_only
            )
        ):
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    (
                        "Поздний flow reversal наблюдается после REJECT; "
                        "Stage 18 не открывает новую позицию на запоздалом REACTION"
                        if late_reaction_only
                        else (
                            "Rejection probe уже открыт; ждём разворот "
                            "локального flow для add"
                            if state.probe_opened
                            else "Пробой не удержался, но локального absorption "
                            "ещё недостаточно для раннего REJECT-входа"
                        )
                    )
                ],
                0.58,
                zone.center,
                visuals=visuals,
                details={
                    "state": (
                        RejectionStage.REACTION.value
                        if late_reaction_only
                        else state.stage.value
                    ),
                    "zone": zone.public(),
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "recentLevelFlow": recent_level_flow.public(),
                    "breakoutFlow": breakout_flow.public(),
                    "roundLevel": round_level,
                    "weakLevel": True,
                    "levelGeneration": generation_id,
                    "formingCandle": (
                        forming.public()
                        if forming is not None
                        else None
                    ),
                    "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
                    "attackAbsorbed": attack_absorbed,
                    "flowReversed": flow_reversed,
                    "lateReactionObserved": late_reaction_only,
                    "probeOpened": state.probe_opened,
                },
            )

        entry_context = assess_entry_context(
            PlaybookKind.LEVEL_REJECTION,
            action,
            market_context,
            trend,
        )
        if not entry_context.allowed:
            return StrategyDecision(
                self.key,
                Action.WAIT,
                [
                    "Отбой подтверждён, но MarketContext не разрешает этот rejection-вход",
                    *entry_context.blockers,
                ],
                0.48,
                zone.center,
                visuals=visuals,
                details={
                    "state": state.stage.value,
                    **context_details,
                    "zone": zone.public(),
                    "flow": flow,
                    "levelFlow": level_flow.public(),
                    "roundLevel": round_level,
                    "weakLevel": True,
                    "levelGeneration": generation_id,
                    "contextAligned": False,
                    "trendAligned": False,
                    "rejectedAction": action.value,
                    "entryContextAssessment": entry_context.public(),
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

        mode = (
            "range_rejection"
            if context_plan.source == "range_two_sided"
            else "trend_following"
        )
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
        if self.staged_entries_enabled:
            probe_fraction = max(
                0.05,
                min(float(self.probe_risk_fraction), 0.80),
            )
            if not flow_reversed:
                staged_phase = "probe"
                staged_risk_fraction = probe_fraction
                state.stage = RejectionStage.REJECT
            elif state.probe_opened:
                staged_phase = "add"
                staged_risk_fraction = 1.0 - probe_fraction
                state.stage = RejectionStage.REACTION
            else:
                staged_phase = "full"
                staged_risk_fraction = 1.0
                state.stage = RejectionStage.REACTION
        else:
            # Smoke evidence showed REJECT carried the edge while waiting for
            # REACTION degraded it. Enter once on early failed-break absorption
            # and do not add later.
            staged_phase = "full"
            staged_risk_fraction = 1.0
            state.stage = RejectionStage.REJECT

        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                f"Слабый уровень: {approaches} отдельных подход(а), без длительной проторговки",
                "Попытка пробоя не удержалась, цена вернулась за границу зоны",
                (
                    "Ранний probe разрешён подтверждённым absorption; "
                    "остаток риска ждёт flow reversal"
                    if staged_phase == "probe"
                    else (
                        "Flow reversal подтвердил probe; добавляем только "
                        "зарезервированный остаток риска"
                        if staged_phase == "add"
                        else (
                            "Failed break + локальное absorption дают ранний "
                            "REJECT-вход без ожидания позднего REACTION"
                            if not self.staged_entries_enabled
                            else "Поток непосредственно у уровня подтвердил разворот/поглощение"
                        )
                    )
                ),
                "Отскок разрешён локальным playbook-контекстом",
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
                "formingCandle": (
                    forming.public()
                    if forming is not None
                    else None
                ),
                "opportunityArm": opportunity_arm,
                    "preparedOpportunity": prepared_opportunity,
                "fireTrigger": {
                    "observedAtMs": (
                        observed_at_ms
                        if observed_at_ms is not None
                        else int(now * 1000)
                    ),
                    "source": (
                        "rejection_absorption_probe"
                        if staged_phase == "probe"
                        else (
                            "rejection_absorption_fire"
                            if not self.staged_entries_enabled
                            else "rejection_flow_reversal"
                        )
                    ),
                    "preparedAtMs": (
                        int(state.armed_at * 1000)
                        if state.armed_at > 0
                        else None
                    ),
                },
                "attackAbsorbed": attack_absorbed,
                "flowReversed": flow_reversed,
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
                    "confirmationReady": (
                        flow_reversed
                        if self.staged_entries_enabled
                        else early_absorption_ready
                    ),
                    "probeOpened": state.probe_opened,
                },
                **context_details,
                "entryContextAssessment": entry_context.public(),
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
                    "contextAligned": entry_context.allowed,
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
        if (
            mode in {"trend_following", "range_rejection"}
            and not position_context_supported(
                PlaybookKind.LEVEL_REJECTION,
                side,
                market_context,
                trend,
            )
        ):
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
        market_context: "MarketContext | None" = None,
        observed_at_ms: int | None = None,
    ) -> StrategyDecision:
        context_plan = rejection_direction_plan(
            market_context,
            trend,
        )
        if (
            len(candles) < 40
            or not symbol
            or not context_plan.allowed_directions
        ):
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["MarketContext пока не разрешает level-rejection playbook"],
                details={
                    "state": RejectionStage.SEARCH.value,
                    "playbookContext": context_plan.public(),
                    "legacyTrend": trend.value,
                },
            )

        price = book.mid or candles[-1].close
        if price <= 0:
            return StrategyDecision(self.key, Action.WAIT, ["Нет текущей цены"])

        state = self._states.setdefault(symbol, RejectionWatchState())
        now = (
            observed_at_ms / 1000
            if observed_at_ms is not None
            else (
                (trades or [])[-1].ts_ms / 1000
                if trades
                else candles[-1].start_ms / 1000
            )
        )
        if (
            state.pinned_zone is not None
            and state.stage in {RejectionStage.TEST, RejectionStage.REJECT}
            and now <= state.pinned_until
            and abs(state.pinned_zone.center - price) / price
            <= self.test_pin_max_distance_pct
        ):
            return self._decision_for_zone(
                candles,
                book,
                trend,
                trades or [],
                symbol,
                state.pinned_zone,
                structure,
                None,
                observed_at_ms=observed_at_ms,
                generation_id_override=state.pinned_generation_id,
                market_context=market_context,
            )
        if state.pinned_zone is not None and (
            now > state.pinned_until
            or abs(state.pinned_zone.center - price) / price
            > self.test_pin_max_distance_pct
        ):
            state.pinned_zone = None
            state.pinned_generation_id = None
            state.pinned_until = 0.0
            state.swept = False
            state.armed_at = 0.0
            state.armed_price = 0.0
            state.probe_opened = False
            state.stage = RejectionStage.SEARCH
            state.zone_key = None

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
        allowed_kinds = set()
        if Trend.UP in context_plan.allowed_directions:
            allowed_kinds.add("support")
        if Trend.DOWN in context_plan.allowed_directions:
            allowed_kinds.add("resistance")
        all_choices = [
            zone
            for zone in (resistance, support)
            if zone is not None
        ]
        allowed_choices = [
            zone
            for zone in all_choices
            if zone.kind in allowed_kinds
        ]
        if not all_choices:
            state.stage = RejectionStage.SEARCH
            state.zone_key = None
            state.armed_at = 0.0
            state.armed_price = 0.0
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Рядом нет молодого слабонаторгованного уровня"],
                details={
                    "state": RejectionStage.SEARCH.value,
                    "playbookContext": context_plan.public(),
                    "legacyTrend": trend.value,
                },
            )

        # Prefer a context-allowed level, but keep observing a nearby
        # counter-context rejection when no allowed alternative exists.
        # This preserves research visibility without making it tradeable.
        choices = allowed_choices or all_choices
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
            market_context=market_context,
        )
