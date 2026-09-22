from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

from ..domain import Action, Candle, OrderBook, StrategyDecision, TradeTick, Trend
from .base import Strategy
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


class BreakoutStage(StrEnum):
    SEARCH = "search"
    FOUND = "found"
    APPROACH = "approach"
    PRESSURE = "pressure"
    BREAK = "break"
    IMPULSE = "impulse"


@dataclass(slots=True)
class BreakoutWatchState:
    zone_key: tuple[str, int, float] | None = None
    stage: BreakoutStage = BreakoutStage.SEARCH
    used_generations: set[tuple[str, int, float]] = field(default_factory=set)


class LevelBreakoutStrategy(Strategy):
    key = "level_breakout"
    label = "Пробой наторгованного уровня"

    min_zone_touches = 5
    approach_pct = 0.0045
    max_stop_pct = 0.006
    max_zone_distance_pct = 0.012
    min_pressure_score = 3

    def __init__(self) -> None:
        self._states: dict[str, BreakoutWatchState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    @staticmethod
    def _generation(zone: LevelZone) -> tuple[str, int, float]:
        return (zone.kind, zone.last_touch_index, round(zone.center, 8))

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

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
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
        state.zone_key = generation
        visuals = zone_visual(zone, "breakout zone")
        flow = compute_trade_flow(trades)
        pressure_score, pressure = self._pressure_score(candles, zone, flow, long_side=long_side)

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
            flow["imbalance5s"] >= 0.05
            if long_side
            else flow["imbalance5s"] <= -0.05
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
            target = entry + expected_impulse
            stop_pct = (entry - stop) / entry
            action = Action.LONG
        else:
            stop = zone.high + range_abs * 0.25
            expected_impulse = max(zone.width * 1.3, range_abs * 2.0)
            target = entry - expected_impulse
            stop_pct = (stop - entry) / entry
            action = Action.SHORT

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

        touch_quality = clamp((zone.touches - self.min_zone_touches + 1) / 4.0)
        pressure_quality = clamp(pressure_score / 5.0)
        flow_quality = clamp(abs(flow["imbalance5s"]) / 0.25)
        reaction_quality = clamp(zone.reaction_pct / max(typical_range_pct(candles), 1e-9) / 2.0)
        quality = clamp(
            0.40
            + touch_quality * 0.20
            + pressure_quality * 0.18
            + flow_quality * 0.12
            + reaction_quality * 0.10
        )

        state.stage = BreakoutStage.IMPULSE
        state.used_generations.add(generation)
        setup_id = (
            f"{self.key}:{action.value}:{zone.kind}:"
            f"{zone.last_touch_index}:{zone.center:.10g}"
        )
        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                "Пробой зрелой наторгованной горизонтальной зоны",
                f"Зона подтверждена {zone.touches} касаниями, реакциями и объёмом",
                "Подход сформировал давление, поток подтверждает сторону пробоя",
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
                "flow": flow,
                "pressure": pressure,
                "pressureScore": pressure_score,
                "stopDistancePct": stop_pct,
                "exitMode": "impulse_first",
                "tradeMode": "trend_following",
                "allowRunner": True,
                "setupQuality": quality,
                "qualityFactors": {
                    "touchMaturity": touch_quality,
                    "pressure": pressure_quality,
                    "flow": flow_quality,
                    "reactionHistory": reaction_quality,
                },
            },
            setup_id=setup_id,
        )
