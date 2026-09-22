from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median
from time import monotonic
from typing import Literal

from ..domain import Action, Candle, OrderBook, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
    from .structure import MarketStructure
from .common import (
    clamp,
    compute_trade_flow,
    nearby_round_level,
    price_visual,
    trade_mode,
    typical_range_abs,
)
from .liquidity import find_liquidity_target


class DensityStage(StrEnum):
    SEARCH = "search"
    FOUND = "found"
    PERSISTING = "persisting"
    APPROACH = "approach"
    TEST = "test"
    DEFENDED = "defended"
    EXHAUSTED = "exhausted"
    REACTION = "reaction"


@dataclass(slots=True)
class DensityWallState:
    side: Literal["bid", "ask"] | None = None
    price: float | None = None
    stage: DensityStage = DensityStage.SEARCH
    first_seen: float = 0.0
    peak_notional: float = 0.0
    current_notional: float = 0.0
    approaches: int = 0
    was_near: bool = False
    touched: bool = False
    defended_at: float = 0.0
    exhausted_until: float = 0.0
    observations: list[tuple[float, float]] = field(default_factory=list)


class DensityBounceStrategy(Strategy):
    key = "orderbook_density"
    label = "Отскок от свежей плотности"

    strength_multiple = 4.0
    max_distance_pct = 0.004
    approach_pct = 0.0022
    approach_reset_pct = 0.0035
    touch_pct = 0.00030
    reaction_pct = 0.00035
    min_persistence_seconds = 3.0
    min_remaining_ratio = 0.70
    max_approaches = 2
    max_depletion_per_second = 0.10
    max_consumption_attack_ratio = 0.35
    max_stop_pct = 0.005
    exhausted_cooldown_seconds = 8.0

    def __init__(self) -> None:
        self._states: dict[str, DensityWallState] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    @staticmethod
    def _rows(book: OrderBook) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], float]:
        bids = [(p, q, p * q) for p, q in book.bids[:25]]
        asks = [(p, q, p * q) for p, q in book.asks[:25]]
        notionals = [row[2] for row in bids + asks]
        return bids, asks, median(notionals) if notionals else 0.0

    @staticmethod
    def _same_price(left: float, right: float) -> bool:
        return abs(left - right) / max(abs(left), abs(right), 1e-12) <= 1e-9

    def _current_wall_notional(
        self,
        state: DensityWallState,
        bids: list[tuple[float, float, float]],
        asks: list[tuple[float, float, float]],
    ) -> float | None:
        if state.side is None or state.price is None:
            return None
        rows = bids if state.side == "bid" else asks
        for price, _qty, notional in rows:
            if self._same_price(price, state.price):
                return notional
        return None

    def _select_wall(
        self,
        book: OrderBook,
        bids: list[tuple[float, float, float]],
        asks: list[tuple[float, float, float]],
        baseline: float,
    ) -> tuple[str, float, float, float] | None:
        mid = book.mid
        if not mid or baseline <= 0:
            return None
        candidates: list[tuple[str, float, float, float, float]] = []
        for side, rows in (("bid", bids), ("ask", asks)):
            for price, _qty, notional in rows:
                strength = notional / baseline
                if strength < self.strength_multiple:
                    continue
                distance = (mid - price) / mid if side == "bid" else (price - mid) / mid
                if 0 <= distance <= self.max_distance_pct:
                    candidates.append((side, price, notional, strength, distance))
        if not candidates:
            return None
        side, price, notional, strength, _ = min(candidates, key=lambda row: (row[4], -row[3]))
        return side, price, notional, strength

    @staticmethod
    def _wall_attack_notional(
        trades: list[TradeTick],
        wall_side: str,
        wall_price: float,
        now_ms: int | None = None,
    ) -> float:
        if not trades:
            return 0.0
        if now_ms is None:
            now_ms = trades[-1].ts_ms
        start = now_ms - 5_000
        attack_side = "buy" if wall_side == "ask" else "sell"
        return sum(
            trade.notional
            for trade in trades
            if trade.ts_ms >= start
            and trade.side.lower() == attack_side
            and abs(trade.price - wall_price) / wall_price <= 0.0006
        )

    @staticmethod
    def _recent_touch(trades: list[TradeTick], wall_price: float, touch_pct: float) -> bool:
        if not trades:
            return False
        now_ms = trades[-1].ts_ms
        return any(
            trade.ts_ms >= now_ms - 5_000
            and abs(trade.price - wall_price) / wall_price <= touch_pct
            for trade in trades
        )

    @staticmethod
    def _record_observation(state: DensityWallState, now: float, notional: float) -> None:
        state.observations.append((now, notional))
        state.observations = [row for row in state.observations if row[0] >= now - 8.0]

    @staticmethod
    def _depletion_per_second(state: DensityWallState) -> float:
        if len(state.observations) < 2 or state.peak_notional <= 0:
            return 0.0
        first_t, first_n = state.observations[0]
        last_t, last_n = state.observations[-1]
        elapsed = max(last_t - first_t, 1e-6)
        return max(0.0, first_n - last_n) / state.peak_notional / elapsed

    @staticmethod
    def _replenishment_ratio(state: DensityWallState) -> float:
        if not state.observations or state.peak_notional <= 0:
            return 0.0
        minimum = min(row[1] for row in state.observations)
        return max(0.0, state.current_notional - minimum) / state.peak_notional

    def _wait(
        self,
        state: DensityWallState,
        reason: str,
        *,
        confidence: float = 0.0,
        details: dict | None = None,
    ) -> StrategyDecision:
        visuals = (
            price_visual(f"{state.side} density", state.price, f"density_{state.side}")
            if state.side and state.price is not None
            else {}
        )
        payload = {
            "state": state.stage.value,
            "wallSide": state.side,
            "wallPrice": state.price,
            "peakNotional": state.peak_notional,
            "currentNotional": state.current_notional,
            "approaches": state.approaches,
        }
        if details:
            payload.update(details)
        return StrategyDecision(
            self.key,
            Action.WAIT,
            [reason],
            confidence,
            state.price,
            visuals=visuals,
            details=payload,
        )

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
        if not candles or not book.bids or not book.asks or trend == Trend.FLAT or not symbol:
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Нужен читаемый тренд, стакан и активная монета"],
                details={"state": DensityStage.SEARCH.value},
            )

        mid = book.mid
        if not mid:
            return StrategyDecision(self.key, Action.WAIT, ["Нет mid price"])

        trades = trades or []
        bids, asks, baseline = self._rows(book)
        if baseline <= 0:
            return StrategyDecision(self.key, Action.WAIT, ["Стакан пуст"])

        now = monotonic()
        state = self._states.setdefault(symbol, DensityWallState())

        if state.stage == DensityStage.EXHAUSTED and now < state.exhausted_until:
            return self._wait(state, "Плотность помечена как съедаемая; ждём новую структуру")
        if state.stage == DensityStage.EXHAUSTED and now >= state.exhausted_until:
            state = DensityWallState()
            self._states[symbol] = state

        wall_present = True
        strength = 0.0
        if state.side is not None and state.price is not None:
            current = self._current_wall_notional(state, bids, asks)
            if current is None:
                wall_present = False
                if state.defended_at <= 0:
                    state.stage = DensityStage.EXHAUSTED
                    state.exhausted_until = now + self.exhausted_cooldown_seconds
                    return self._wait(
                        state,
                        "Плотность снята до подтверждённой защиты — вход отменён",
                        details={"reason": "wall_removed_before_defense"},
                    )
                state.current_notional = 0.0
            else:
                state.current_notional = current
                state.peak_notional = max(state.peak_notional, current)
                strength = current / baseline
                self._record_observation(state, now, current)
        else:
            selected = self._select_wall(book, bids, asks, baseline)
            if selected is None:
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    ["Свежей крупной плотности рядом с ценой нет"],
                    details={"state": DensityStage.SEARCH.value},
                )
            side, price, notional, strength = selected
            state = DensityWallState(
                side=side,
                price=price,
                stage=DensityStage.FOUND,
                first_seen=now,
                peak_notional=notional,
                current_notional=notional,
            )
            self._record_observation(state, now, notional)
            self._states[symbol] = state

        wall_price = float(state.price)
        flow = compute_trade_flow(trades)
        remaining_ratio = (
            state.current_notional / state.peak_notional if state.peak_notional > 0 else 0.0
        )
        attack_notional = self._wall_attack_notional(trades, str(state.side), wall_price)
        attack_ratio = attack_notional / state.peak_notional if state.peak_notional > 0 else 0.0
        depletion_rate = self._depletion_per_second(state)
        replenishment_ratio = self._replenishment_ratio(state)
        absorption = attack_ratio >= 0.05 and remaining_ratio >= 0.80
        consuming = (
            remaining_ratio < self.min_remaining_ratio
            or depletion_rate > self.max_depletion_per_second
            or (
                attack_ratio >= self.max_consumption_attack_ratio
                and remaining_ratio < 0.85
            )
        )

        invalidation_buffer = max(0.0004, book.spread_pct * 2.0)
        if state.side == "ask":
            price_crossed = mid > wall_price * (1 + invalidation_buffer)
            flow_through = flow["imbalance5s"] >= 0.15
        else:
            price_crossed = mid < wall_price * (1 - invalidation_buffer)
            flow_through = flow["imbalance5s"] <= -0.15
        position_invalidated = state.defended_at > 0 and price_crossed and flow_through

        distance_pct = abs(mid - wall_price) / mid if mid > 0 else 0.0
        erosion_start = next(
            (
                ts
                for ts, notional in state.observations
                if state.peak_notional > 0 and notional <= state.peak_notional * 0.90
            ),
            None,
        )
        erosion_seconds = max(0.0, now - erosion_start) if erosion_start is not None else 0.0
        round_confluence = nearby_round_level(
            wall_price, wall_price * 0.0003
        ) is not None

        shared = {
            "remainingRatio": remaining_ratio,
            "attackNotional5s": attack_notional,
            "attackRatio": attack_ratio,
            "depletionPerSecond": depletion_rate,
            "replenishmentRatio": replenishment_ratio,
            "absorptionObserved": absorption,
            "wallPresent": wall_present,
            "flow": flow,
            "positionInvalidated": position_invalidated,
            "distancePct": distance_pct,
            "lifetimeSeconds": max(0.0, now - state.first_seen),
            "erosionSeconds": erosion_seconds,
            "roundConfluence": round_confluence,
            "notionalUsd": state.current_notional,
        }

        if state.defended_at > 0 and not wall_present:
            state.stage = DensityStage.REACTION
            return self._wait(
                state,
                "После подтверждённого отбоя wall снята; управляем позицией по цене и потоку",
                confidence=0.55,
                details=shared,
            )

        if state.defended_at <= 0 and consuming:
            state.stage = DensityStage.EXHAUSTED
            state.exhausted_until = now + self.exhausted_cooldown_seconds
            return self._wait(
                state,
                "Плотность реально съедают — отскок не торгуем",
                details=shared,
            )

        age = now - state.first_seen
        if age < self.min_persistence_seconds or len(state.observations) < 3:
            state.stage = DensityStage.PERSISTING
            return self._wait(
                state,
                "Плотность новая: проверяем устойчивость во времени",
                confidence=0.35,
                details=shared,
            )

        distance = (
            (mid - wall_price) / mid
            if state.side == "bid"
            else (wall_price - mid) / mid
        )
        near = 0 <= distance <= self.approach_pct
        if near and not state.was_near:
            state.approaches += 1
        if distance >= self.approach_reset_pct:
            state.was_near = False
        elif near:
            state.was_near = True

        if state.approaches > self.max_approaches and state.defended_at <= 0:
            state.stage = DensityStage.EXHAUSTED
            state.exhausted_until = now + self.exhausted_cooldown_seconds
            return self._wait(
                state,
                "К плотности уже слишком много раз подходили — свежесть потеряна",
                details=shared,
            )

        if not near and state.defended_at <= 0:
            state.stage = DensityStage.FOUND
            return self._wait(
                state,
                "Устойчивая плотность найдена, ждём подход",
                confidence=0.42,
                details=shared,
            )

        if state.defended_at <= 0:
            state.stage = DensityStage.APPROACH
            touched = self._recent_touch(trades, wall_price, max(self.touch_pct, book.spread_pct * 1.5))
            if touched:
                state.touched = True
                state.stage = DensityStage.TEST

            if not state.touched:
                return self._wait(
                    state,
                    "Цена подходит к свежей плотности, ждём фактический тест",
                    confidence=0.50,
                    details=shared,
                )

        if state.side == "bid":
            action = Action.LONG
            reacted = mid >= wall_price * (1 + self.reaction_pct)
            flow_reversed = flow["tradeCount5s"] >= 3 and flow["imbalance5s"] >= 0.03
        else:
            action = Action.SHORT
            reacted = mid <= wall_price * (1 - self.reaction_pct)
            flow_reversed = flow["tradeCount5s"] >= 3 and flow["imbalance5s"] <= -0.03

        if not (reacted and flow_reversed):
            state.stage = DensityStage.DEFENDED if state.touched else DensityStage.APPROACH
            return self._wait(
                state,
                "Плотность выдерживает тест; ждём движение цены и потока от wall",
                confidence=0.60,
                details=shared,
            )

        state.stage = DensityStage.REACTION
        if state.defended_at <= 0:
            state.defended_at = now

        range_abs = typical_range_abs(candles)
        stop_buffer = max(
            range_abs * 0.12,
            wall_price * max(book.spread_pct * 1.5, 0.00012),
        )
        if action == Action.LONG:
            stop = wall_price - stop_buffer
            risk = mid - stop
        else:
            stop = wall_price + stop_buffer
            risk = stop - mid

        if risk <= 0 or risk / mid > self.max_stop_pct:
            return self._wait(
                state,
                "Отбой подтверждён, но структурный stop слишком далеко для скальпа",
                confidence=0.45,
                details=shared,
            )

        mode, allow_runner = trade_mode(action, trend)
        target_r = 1.6 if allow_runner else 0.75
        reaction_target = (
            mid + risk * target_r
            if action == Action.LONG
            else mid - risk * target_r
        )
        liquidity_target = find_liquidity_target(candles, mid, action, structure=structure)
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

        strength_score = clamp((strength - self.strength_multiple) / 6.0)
        stability_score = clamp((remaining_ratio - 0.70) / 0.30)
        flow_score = clamp(abs(flow["imbalance5s"]) / 0.25)
        absorption_score = 1.0 if absorption else clamp(replenishment_ratio / 0.20)
        quality = clamp(
            0.40
            + strength_score * 0.15
            + stability_score * 0.20
            + flow_score * 0.15
            + absorption_score * 0.10
            + (0.05 if allow_runner else 0.0)
        )

        visuals = price_visual(
            f"{state.side} defended density",
            wall_price,
            f"density_{state.side}",
        )
        details = dict(shared)
        details.update(
            {
                "state": state.stage.value,
                "wallSide": state.side,
                "wallPrice": wall_price,
                "peakNotional": state.peak_notional,
                "currentNotional": state.current_notional,
                "approaches": state.approaches,
                "strengthMultiple": strength,
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
                "densityFresh": True,
                "setupQuality": quality,
                "qualityFactors": {
                    "strength": strength_score,
                    "stability": stability_score,
                    "flow": flow_score,
                    "absorption": absorption_score,
                    "trendAligned": allow_runner,
                },
            }
        )

        return StrategyDecision(
            strategy=self.key,
            action=action,
            reasons=[
                f"Свежая wall {strength:.1f}x к медиане стакана",
                f"Wall сохранила {remaining_ratio * 100:.0f}% пикового объёма",
                (
                    "Агрессивные сделки поглощаются с replenishment"
                    if absorption
                    else "Wall стабильна и не показывает быстрого depletion"
                ),
                "Цена протестировала wall и поток развернулся от неё",
                (
                    "Отскок по тренду: runner разрешён"
                    if allow_runner
                    else "Отскок против тренда: только короткая реакция"
                ),
            ],
            confidence=quality,
            watched_level=wall_price,
            entry=mid,
            stop=stop,
            target=target,
            visuals=visuals,
            details=details,
        )
