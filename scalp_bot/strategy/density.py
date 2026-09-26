from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median
from typing import Literal

from ..domain import Action, Candle, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .base import Strategy

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .structure import MarketStructure
from .common import (
    clamp,
    compute_trade_flow,
    nearby_round_level,
    price_visual,
    typical_range_abs,
)
from .flow import flow_at_level
from .liquidity import find_liquidity_targets


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
    label = "Ликвидность стакана (evidence)"

    strength_multiple = 4.0
    min_wall_notional_usd = 25_000.0
    turnover_floor_fraction = 0.01
    neighbor_window_levels = 20
    max_distance_pct = 0.05
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

    @classmethod
    def _reaction_confirmation(
        cls,
        side: str,
        mid: float,
        wall_price: float,
        *,
        participation_confirmed: bool,
        recent_trade_count: int,
        recent_imbalance: float,
    ) -> dict:
        if side == "bid":
            reacted = mid >= wall_price * (1 + cls.reaction_pct)
            flow_reversed = (
                participation_confirmed
                and recent_trade_count >= 3
                and recent_imbalance >= 0.03
            )
        else:
            reacted = mid <= wall_price * (1 - cls.reaction_pct)
            flow_reversed = (
                participation_confirmed
                and recent_trade_count >= 3
                and recent_imbalance <= -0.03
            )
        return {
            "priceReactionConfirmed": reacted,
            "flowReversalConfirmed": flow_reversed,
            "entryConfirmationComplete": reacted and flow_reversed,
        }

    @staticmethod
    def _rows(
        book: OrderBook,
    ) -> tuple[
        list[tuple[float, float, float]],
        list[tuple[float, float, float]],
        float,
    ]:
        bids = [(p, q, p * q) for p, q in book.bids]
        asks = [(p, q, p * q) for p, q in book.asks]
        notionals = [row[2] for row in bids + asks]
        return bids, asks, median(notionals) if notionals else 0.0

    @staticmethod
    def _coverage(book: OrderBook) -> dict:
        mid = book.mid
        if not mid or not book.bids or not book.asks:
            return {
                "bidCoveragePct": 0.0,
                "askCoveragePct": 0.0,
                "bidLevels": len(book.bids),
                "askLevels": len(book.asks),
            }
        lowest_bid = min(price for price, _ in book.bids)
        highest_ask = max(price for price, _ in book.asks)
        return {
            "bidCoveragePct": max(0.0, (mid - lowest_bid) / mid),
            "askCoveragePct": max(0.0, (highest_ask - mid) / mid),
            "bidLevels": len(book.bids),
            "askLevels": len(book.asks),
        }

    @staticmethod
    def _wall_is_observable(
        state: DensityWallState,
        book: OrderBook,
    ) -> bool:
        if state.side is None or state.price is None:
            return False
        if state.side == "bid":
            if not book.bids:
                return False
            return state.price >= min(price for price, _ in book.bids)
        if not book.asks:
            return False
        return state.price <= max(price for price, _ in book.asks)

    @staticmethod
    def _same_price(left: float, right: float) -> bool:
        return abs(left - right) / max(abs(left), abs(right), 1e-12) <= 1e-9

    def _local_baseline(
        self,
        rows: list[tuple[float, float, float]],
        index: int,
    ) -> float:
        radius = max(1, int(self.neighbor_window_levels))
        start = max(0, index - radius)
        end = min(len(rows), index + radius + 1)
        values = [
            rows[i][2]
            for i in range(start, end)
            if i != index and rows[i][2] > 0
        ]
        if not values:
            values = [
                row[2]
                for i, row in enumerate(rows)
                if i != index and row[2] > 0
            ]
        return median(values) if values else 0.0

    def _current_wall_metrics(
        self,
        state: DensityWallState,
        bids: list[tuple[float, float, float]],
        asks: list[tuple[float, float, float]],
        absolute_wall_floor: float,
    ) -> tuple[float, float, float, float] | None:
        if state.side is None or state.price is None:
            return None
        rows = bids if state.side == "bid" else asks
        for index, (price, _qty, notional) in enumerate(rows):
            if not self._same_price(price, state.price):
                continue
            local_baseline = self._local_baseline(rows, index)
            if local_baseline <= 0:
                return None
            relative_required = local_baseline * self.strength_multiple
            effective_required = max(
                absolute_wall_floor,
                relative_required,
            )
            strength = notional / local_baseline
            return (
                notional,
                local_baseline,
                effective_required,
                strength,
            )
        return None

    def _select_wall(
        self,
        book: OrderBook,
        bids: list[tuple[float, float, float]],
        asks: list[tuple[float, float, float]],
        baseline: float,
        absolute_wall_floor: float,
    ) -> tuple[str, float, float, float, float, float] | None:
        mid = book.mid
        if not mid or baseline <= 0:
            return None
        candidates: list[
            tuple[str, float, float, float, float, float, float]
        ] = []
        for side, rows in (("bid", bids), ("ask", asks)):
            for index, (price, _qty, notional) in enumerate(rows):
                local_baseline = self._local_baseline(rows, index)
                if local_baseline <= 0:
                    continue
                relative_required = local_baseline * self.strength_multiple
                effective_required = max(
                    absolute_wall_floor,
                    relative_required,
                )
                strength = notional / local_baseline
                if notional < effective_required:
                    continue
                distance = (
                    (mid - price) / mid
                    if side == "bid"
                    else (price - mid) / mid
                )
                if 0 <= distance <= self.max_distance_pct:
                    candidates.append(
                        (
                            side,
                            price,
                            notional,
                            strength,
                            distance,
                            local_baseline,
                            effective_required,
                        )
                    )
        if not candidates:
            return None
        (
            side,
            price,
            notional,
            strength,
            _distance,
            local_baseline,
            effective_required,
        ) = min(candidates, key=lambda row: (row[4], -row[3]))
        return (
            side,
            price,
            notional,
            strength,
            local_baseline,
            effective_required,
        )

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
        if decision is None:
            return None
        if bool(decision.details.get("positionInvalidated")):
            return "density_price_flow_invalidated"
        if unrealized_pnl >= 0:
            return None
        mode = str(strategy_details.get("tradeMode") or "")
        expected = Trend.UP if side == Side.LONG else Trend.DOWN
        if mode == "trend_following" and trend != expected:
            return "density_context_lost"
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
        if not candles or not book.bids or not book.asks or not symbol:
            if symbol:
                self.reset(symbol)
            return StrategyDecision(
                self.key,
                Action.WAIT,
                ["Нужны стакан, свечная история и активная монета"],
                details={"state": DensityStage.SEARCH.value},
            )

        mid = book.mid
        if not mid:
            return StrategyDecision(self.key, Action.WAIT, ["Нет mid price"])

        trades = trades or []
        bids, asks, baseline = self._rows(book)
        coverage = self._coverage(book)
        coverage["maxScanDistancePct"] = self.max_distance_pct
        coverage["bidCoverageComplete"] = (
            coverage["bidCoveragePct"] >= self.max_distance_pct
        )
        coverage["askCoverageComplete"] = (
            coverage["askCoveragePct"] >= self.max_distance_pct
        )
        coverage["coverageComplete"] = (
            coverage["bidCoverageComplete"]
            and coverage["askCoverageComplete"]
        )
        if baseline <= 0:
            return StrategyDecision(self.key, Action.WAIT, ["Стакан пуст"])

        recent_turnovers = [c.turnover for c in candles[-20:] if c.turnover > 0]
        typical_minute_turnover = (
            median(recent_turnovers)
            if recent_turnovers
            else 0.0
        )
        turnover_floor = (
            typical_minute_turnover
            * max(0.0, self.turnover_floor_fraction)
        )
        absolute_wall_floor = max(
            self.min_wall_notional_usd,
            turnover_floor,
        )

        if observed_at_ms is not None:
            now = observed_at_ms / 1000
        elif trades:
            now = trades[-1].ts_ms / 1000
        else:
            now = candles[-1].start_ms / 1000
        state = self._states.setdefault(symbol, DensityWallState())

        if state.stage == DensityStage.EXHAUSTED and now < state.exhausted_until:
            return self._wait(
                state,
                "Плотность исчерпана; ждём новую структуру",
                details={"reason": "wall_exhausted_cooldown"},
            )
        if state.stage == DensityStage.EXHAUSTED and now >= state.exhausted_until:
            state = DensityWallState()
            self._states[symbol] = state

        wall_present = True
        strength = 0.0
        local_baseline = 0.0
        effective_required = absolute_wall_floor
        if state.side is not None and state.price is not None:
            metrics = self._current_wall_metrics(
                state,
                bids,
                asks,
                absolute_wall_floor,
            )
            if metrics is None:
                if not self._wall_is_observable(state, book):
                    return self._wait(
                        state,
                        "Плотность вышла за границу текущего стакана; её статус неизвестен",
                        confidence=0.20,
                        details={
                            "reason": "wall_outside_book_coverage",
                            "bookCoverage": coverage,
                            "positionInvalidated": False,
                        },
                    )
                wall_present = False
                if state.defended_at <= 0:
                    state.stage = DensityStage.EXHAUSTED
                    state.exhausted_until = now + self.exhausted_cooldown_seconds
                    return self._wait(
                        state,
                        "Плотность снята до подтверждённой защиты — вход отменён",
                        details={
                            "reason": "wall_removed_before_defense",
                            "bookCoverage": coverage,
                        },
                    )
                state.current_notional = 0.0
            else:
                (
                    current,
                    local_baseline,
                    effective_required,
                    strength,
                ) = metrics
                state.current_notional = current
                state.peak_notional = max(state.peak_notional, current)
                self._record_observation(state, now, current)
        else:
            selected = self._select_wall(
                book,
                bids,
                asks,
                baseline,
                absolute_wall_floor,
            )
            if selected is None:
                reason = (
                    "Крупная плотность не найдена в доступной части стакана; "
                    "полный диапазон сканирования не покрыт"
                    if not coverage["coverageComplete"]
                    else "Свежей крупной плотности в заданном диапазоне нет"
                )
                return StrategyDecision(
                    self.key,
                    Action.WAIT,
                    [reason],
                    details={
                        "state": DensityStage.SEARCH.value,
                        "bookCoverage": coverage,
                        "coverageIncomplete": not coverage["coverageComplete"],
                    },
                )
            (
                side,
                price,
                notional,
                strength,
                local_baseline,
                effective_required,
            ) = selected
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
        flow = compute_trade_flow(trades, observed_at_ms) if trade_flow is None else trade_flow
        level_tolerance = max(self.touch_pct * 2, 0.0006)
        level_flow = flow_at_level(
            trades,
            wall_price,
            tolerance_pct=level_tolerance,
            seconds=15,
            now_ms=observed_at_ms,
        )
        recent_level_flow = flow_at_level(
            trades,
            wall_price,
            tolerance_pct=level_tolerance,
            seconds=5,
            now_ms=observed_at_ms,
        )
        remaining_ratio = (
            state.current_notional / state.peak_notional if state.peak_notional > 0 else 0.0
        )
        attack_notional = self._wall_attack_notional(trades, str(state.side), wall_price)
        attack_ratio = attack_notional / state.peak_notional if state.peak_notional > 0 else 0.0
        depletion_rate = self._depletion_per_second(state)
        replenishment_ratio = self._replenishment_ratio(state)
        absorption = (
            attack_ratio >= 0.05 and remaining_ratio >= 0.80
        ) or level_flow.absorption_efficiency >= 0.35
        lost_significance = (
            state.current_notional > 0
            and state.current_notional < effective_required
        )
        consuming = (
            remaining_ratio < self.min_remaining_ratio
            or depletion_rate > self.max_depletion_per_second
            or (
                attack_ratio >= self.max_consumption_attack_ratio
                and remaining_ratio < 0.85
            )
        )
        consumption_cause: list[str] = []
        if remaining_ratio < self.min_remaining_ratio:
            consumption_cause.append("remaining_ratio")
        if depletion_rate > self.max_depletion_per_second:
            consumption_cause.append("depletion_rate")
        if (
            attack_ratio >= self.max_consumption_attack_ratio
            and remaining_ratio < 0.85
        ):
            consumption_cause.append("aggressive_attack")

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
            "lostSignificance": lost_significance,
            "consuming": consuming,
            "consumptionCause": consumption_cause,
            "flow": flow,
            "positionInvalidated": position_invalidated,
            "distancePct": distance_pct,
            "lifetimeSeconds": max(0.0, now - state.first_seen),
            "erosionSeconds": erosion_seconds,
            "roundConfluence": round_confluence,
            "notionalUsd": state.current_notional,
            "configuredMinWallUsd": self.min_wall_notional_usd,
            "typicalMinuteTurnoverUsd": typical_minute_turnover,
            "turnoverFloorFraction": self.turnover_floor_fraction,
            "turnoverFloorUsd": turnover_floor,
            "localBaselineNotionalUsd": local_baseline,
            "strengthMultiple": strength,
            "requiredStrengthMultiple": self.strength_multiple,
            "relativeRequiredNotionalUsd": (
                local_baseline * self.strength_multiple
            ),
            "effectiveWallFloorUsd": effective_required,
            "absoluteWallFloorUsd": absolute_wall_floor,
            "bookCoverage": coverage,
            "coverageIncomplete": not coverage["coverageComplete"],
            "levelFlow": level_flow.public(),
            "recentLevelFlow": recent_level_flow.public(),
        }

        if state.defended_at > 0 and not wall_present:
            state.stage = DensityStage.REACTION
            return self._wait(
                state,
                "После подтверждённого отбоя wall снята; управляем позицией по цене и потоку",
                confidence=0.55,
                details={
                    **shared,
                    "reason": "wall_removed_after_defense",
                },
            )

        if state.defended_at <= 0 and lost_significance:
            state.stage = DensityStage.EXHAUSTED
            state.exhausted_until = now + self.exhausted_cooldown_seconds
            return self._wait(
                state,
                "Плотность перестала быть значимой относительно локального стакана/активности",
                details={
                    "bookCoverage": coverage,
                    "localBaselineNotionalUsd": local_baseline,
                    "effectiveWallFloorUsd": effective_required,
                    "absoluteWallFloorUsd": absolute_wall_floor,
                    "turnoverFloorUsd": turnover_floor,
                    "configuredMinWallUsd": self.min_wall_notional_usd,
                    "strengthMultiple": strength,
                    "reason": "wall_lost_significance",
                },
            )

        if state.defended_at <= 0 and consuming:
            state.stage = DensityStage.EXHAUSTED
            state.exhausted_until = now + self.exhausted_cooldown_seconds
            return self._wait(
                state,
                "Плотность реально съедают — отскок не торгуем",
                details={
                    **shared,
                    "reason": "wall_consumed_before_defense",
                },
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
                details={
                    **shared,
                    "reason": "wall_freshness_exhausted",
                },
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

        action = (
            Action.LONG
            if state.side == "bid"
            else Action.SHORT
        )
        reaction_status = self._reaction_confirmation(
            str(state.side),
            mid,
            wall_price,
            participation_confirmed=bool(
                flow["participationConfirmed"]
            ),
            recent_trade_count=recent_level_flow.trade_count,
            recent_imbalance=recent_level_flow.imbalance,
        )
        reacted = bool(
            reaction_status["priceReactionConfirmed"]
        )
        flow_reversed = bool(
            reaction_status["flowReversalConfirmed"]
        )
        if not (reacted and flow_reversed):
            state.stage = DensityStage.TEST if state.touched else DensityStage.APPROACH
            missing = []
            if not reacted:
                missing.append("price reaction")
            if not flow_reversed:
                missing.append("local flow reversal")
            return self._wait(
                state,
                (
                    "Wall протестирована; ждём подтверждение: "
                    + ", ".join(missing)
                ),
                confidence=0.60,
                details={
                    **shared,
                    **reaction_status,
                },
            )

        state.stage = DensityStage.DEFENDED
        if state.defended_at <= 0:
            state.defended_at = now

        if trend == Trend.FLAT:
            state.stage = DensityStage.DEFENDED
            return self._wait(
                state,
                "Wall защищена; HTF нейтрален, используем событие только как liquidity evidence",
                confidence=0.55,
                details={
                    **shared,
                    "trendAligned": None,
                    "evidenceAction": action.value,
                },
            )

        expected_action = Action.LONG if trend == Trend.UP else Action.SHORT
        if action != expected_action:
            state.stage = DensityStage.DEFENDED
            return self._wait(
                state,
                "Плотность защищена, но отбой направлен против HTF тренда; контртрендовый вход запрещён",
                confidence=0.48,
                details={
                    **shared,
                    "trendAligned": False,
                    "rejectedAction": action.value,
                },
            )

        state.stage = DensityStage.REACTION

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

        mode = "trend_following"
        allow_runner = True
        target_r = 1.6
        reaction_target = (
            mid + risk * target_r
            if action == Action.LONG
            else mid - risk * target_r
        )
        liquidity_ladder = find_liquidity_targets(
            candles,
            mid,
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
                if abs(row.price - mid) >= risk * target_r
            ),
            None,
        )
        target = (
            liquidity_target.price
            if liquidity_target is not None
            else reaction_target
        )

        strength_score = clamp((strength - self.strength_multiple) / 6.0)
        stability_score = clamp((remaining_ratio - 0.70) / 0.30)
        flow_score = clamp(abs(level_flow.imbalance) / 0.25)
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
                **reaction_status,
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
                "Отскок подтверждён в направлении HTF тренда",
            ],
            confidence=quality,
            watched_level=wall_price,
            entry=mid,
            stop=stop,
            target=target,
            visuals=visuals,
            details=details,
        )
