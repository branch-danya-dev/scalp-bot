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
    trade_mode,
    typical_range_abs,
    zone_overlap_count,
    zone_visual,
)
from .flow import flow_at_level
from .liquidity import find_liquidity_target


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


class WeakLevelRejectionStrategy(Strategy):
    key = "weak_level_rejection"
    label = "Отбой от слабого уровня"

    max_touches = 3
    approach_pct = 0.005
    max_stop_pct = 0.006

    def __init__(self) -> None:
        self._states: dict[str, RejectionWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

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
        flow = compute_trade_flow(trades)
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
            failed_break = last.high >= zone.high * 0.9995 and last.close < zone.low
            attack_absorbed = (
                level_flow.buy_notional > level_flow.sell_notional
                and level_flow.absorption_efficiency >= 0.30
            )
            flow_reversed = (
                level_flow.trade_count >= 3
                and (
                    level_flow.imbalance <= -0.03
                    or (
                        attack_absorbed
                        and flow["tradeCount5s"] >= 3
                        and flow["imbalance5s"] <= -0.03
                    )
                )
            )
            action = Action.SHORT
            stop_anchor = max(zone.high, round_level or zone.high)
            stop = stop_anchor + buffer
            risk = stop - price
        else:
            tested = last.low <= zone.high
            failed_break = last.low <= zone.low * 1.0005 and last.close > zone.high
            attack_absorbed = (
                level_flow.sell_notional > level_flow.buy_notional
                and level_flow.absorption_efficiency >= 0.30
            )
            flow_reversed = (
                level_flow.trade_count >= 3
                and (
                    level_flow.imbalance >= 0.03
                    or (
                        attack_absorbed
                        and flow["tradeCount5s"] >= 3
                        and flow["imbalance5s"] >= 0.03
                    )
                )
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
                    "roundLevel": round_level,
                    "weakLevel": True,
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

        mode, allow_runner = trade_mode(action, trend)
        target_r = 1.6 if allow_runner else 0.75
        reaction_target = (
            price + risk * target_r
            if action == Action.LONG
            else price - risk * target_r
        )
        liquidity_target = find_liquidity_target(candles, price, action, structure=structure)
        if allow_runner and liquidity_target is not None:
            target = liquidity_target.price
        elif not allow_runner and liquidity_target is not None:
            target = (
                min(reaction_target, liquidity_target.price)
                if action == Action.LONG
                else max(reaction_target, liquidity_target.price)
            )
        else:
            target = reaction_target

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
        state.used_generations.add(generation_id)

        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                f"Слабый уровень: {approaches} отдельных подход(а), без длительной проторговки",
                "Попытка пробоя не удержалась, цена вернулась за границу зоны",
                "Поток непосредственно у уровня подтвердил разворот/поглощение",
                (
                    "Отскок идёт по тренду: runner разрешён"
                    if allow_runner
                    else "Отскок против тренда: берём только короткую реакцию"
                ),
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
                "exitMode": "runner_allowed" if allow_runner else "reaction_only",
                "targetR": target_r,
                "liquidityTarget": (
                    liquidity_target.public() if liquidity_target else None
                ),
                "targetSource": (
                    "liquidity"
                    if allow_runner and liquidity_target is not None
                    else "reaction_cap"
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
    ) -> str | None:
        if unrealized_pnl >= 0:
            return None
        mode = str(strategy_details.get("tradeMode") or "")
        expected = Trend.UP if side == Side.LONG else Trend.DOWN
        if mode == "trend_following" and trend != expected:
            return "weak_level_context_lost"
        zone = (
            strategy_details.get("zone")
            if isinstance(strategy_details, dict)
            else None
        )
        if isinstance(zone, dict):
            low = float(zone.get("low") or 0)
            high = float(zone.get("high") or 0)
            if side == Side.LONG and low > 0 and last_price < low:
                return "weak_level_invalidated"
            if side == Side.SHORT and high > 0 and last_price > high:
                return "weak_level_invalidated"
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
        )
