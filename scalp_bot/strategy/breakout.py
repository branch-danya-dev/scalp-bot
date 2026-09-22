from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
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
from .flow import flow_at_level
from .liquidity import find_liquidity_target


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


class LevelBreakoutStrategy(Strategy):
    key = "level_breakout"
    label = "Пробой наторгованного уровня"

    min_zone_touches = 5
    min_distinct_approaches = 4
    approach_pct = 0.0045
    max_stop_pct = 0.006
    max_zone_distance_pct = 0.012
    min_pressure_score = 3

    def __init__(self) -> None:
        self._states: dict[str, BreakoutWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

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
            flow_aligned = flow["imbalance5s"] >= 0.08
        else:
            near_count = sum(1 for candle in recent if candle.close <= zone.high * 1.003)
            structure = sum(
                1
                for left, right in zip(recent[-4:-1], recent[-3:])
                if right.high <= left.high
            )
            flow_aligned = flow["imbalance5s"] <= -0.08

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
    ) -> str | None:
        if unrealized_pnl >= 0:
            return None
        expected = Trend.UP if side == Side.LONG else Trend.DOWN
        if trend != expected:
            return "breakout_context_lost"
        zone = (
            strategy_details.get("zone")
            if isinstance(strategy_details, dict)
            else None
        )
        if isinstance(zone, dict):
            low = float(zone.get("low") or 0)
            high = float(zone.get("high") or 0)
            if side == Side.LONG and high > 0 and last_price < high:
                return "breakout_failed_back_inside"
            if side == Side.SHORT and low > 0 and last_price > low:
                return "breakout_failed_back_inside"
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
        if len(candles) < 60 or trend == Trend.FLAT or not symbol:
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Нет направленного контекста для пробоя"],
                details={"state": BreakoutStage.SEARCH.value},
            )

        state = self._states.setdefault(symbol, BreakoutWatchState())
        trades = trades or []
        price = book.mid or candles[-1].close
        long_side = trend == Trend.UP
        zone_kind: LevelKind = "resistance" if long_side else "support"
        if structure is not None:
            structural = [
                level
                for level in structure.levels
                if level.kind == zone_kind
                and level.touches >= self.min_zone_touches
                and level.distinct_approaches >= self.min_distinct_approaches
                and level.reaction_pct >= typical_range_pct(candles) * 0.45
                and level.volume_ratio >= 0.80
                and level.lifecycle == "worked"
            ]
            zones = [level.as_zone() for level in structural]
        else:
            zones = detect_level_zones(
                candles,
                zone_kind,
                min_touches=self.min_zone_touches,
            )
            zones = [zone for zone in zones if self._mature(zone, candles)]
        zone = self._select_zone(zones, price, long_side=long_side)

        if zone is None:
            state.stage = BreakoutStage.SEARCH
            state.zone_key = None
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Зрелая наторгованная зона для пробоя рядом не найдена"],
                details={"state": state.stage.value},
            )

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
        state.zone_key = generation
        visuals = zone_visual(zone, "breakout zone")
        flow = compute_trade_flow(trades)
        level_tolerance = max(
            zone.width_pct * 1.5,
            book.spread_pct * 2.0,
            0.0008,
        )
        level_flow = flow_at_level(
            trades,
            zone.center,
            tolerance_pct=level_tolerance,
            seconds=15,
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
                },
            )

        state.stage = BreakoutStage.BREAK
        aligned_after_break = (
            level_flow.trade_count >= 3
            and (
                level_flow.imbalance >= 0.05
                if long_side
                else level_flow.imbalance <= -0.05
            )
            and (
                level_flow.price_response_pct >= -0.0001
                if long_side
                else level_flow.price_response_pct <= 0.0001
            )
        )
        if pressure_score < self.min_pressure_score or not aligned_after_break:
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
                },
            )

        range_abs = typical_range_abs(candles)
        entry = price
        if long_side:
            stop = zone.low - range_abs * 0.25
            expected_impulse = max(zone.width * 1.3, range_abs * 2.0)
            fallback_target = entry + expected_impulse
            stop_pct = (entry - stop) / entry
            action = Action.LONG
        else:
            stop = zone.high + range_abs * 0.25
            expected_impulse = max(zone.width * 1.3, range_abs * 2.0)
            fallback_target = entry - expected_impulse
            stop_pct = (stop - entry) / entry
            action = Action.SHORT

        liquidity_target = find_liquidity_target(
            candles,
            entry,
            action,
            max_distance_pct=0.06,
            structure=structure,
        )
        target = (
            liquidity_target.price
            if liquidity_target is not None
            else fallback_target
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
        state.used_generations.add(generation)
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
                "pressure": pressure,
                "pressureScore": pressure_score,
                "stopDistancePct": stop_pct,
                "exitMode": "impulse_first",
                "liquidityTarget": (
                    liquidity_target.public() if liquidity_target else None
                ),
                "targetSource": (
                    "liquidity" if liquidity_target else "impulse_fallback"
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
