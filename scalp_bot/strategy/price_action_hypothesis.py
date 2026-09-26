"""Opt-in paper beta: a closed-candle hypothesis with explicit invalidation.

Fixed v1 rules, not fitted to a session and not a calibrated price forecast.
Uses the normal engine, semantic arbitration, risk sizing and paper broker.
"""
from __future__ import annotations

from statistics import median
from typing import TYPE_CHECKING

from ..domain import Action, Candle, OrderBook, StrategyDecision, TradeTick, Trend
from .base import Strategy
from ..scenario_identity import assigned_ref, candle_ref
from .targets import structural_target, movement_budget
from .liquidity import find_liquidity_targets
from .common import compute_trade_flow

if TYPE_CHECKING:
    from .market_context import MarketContext
    from .structure import MarketStructure


class PriceActionHypothesisStrategy(Strategy):
    key = "price_action_hypothesis"
    label = "Гипотеза цены · BETA"
    minimum_volume_ratio = 1.2
    maximum_chase_r = 0.25

    def __init__(self) -> None:
        self._used_bar: dict[str, int] = {}
        self._fire: dict[str, tuple[int, dict]] = {}

    def reset(self, symbol: str) -> None:
        self._used_bar.pop(symbol, None)
        self._fire.pop(symbol, None)

    def mark_opened(self, symbol: str, decision: StrategyDecision) -> None:
        self._used_bar[symbol] = int(decision.details["hypothesis"]["candleStartMs"])

    @staticmethod
    def _pattern(bar: Candle, previous: Candle, direction: int) -> str | None:
        span = bar.high - bar.low
        if span <= 0 or direction * (bar.close - bar.open) <= 0:
            return None
        body = abs(bar.close - bar.open) / span
        upper = (bar.high - max(bar.open, bar.close)) / span
        lower = (min(bar.open, bar.close) - bar.low) / span
        adverse_wick = upper if direction > 0 else lower
        supportive_wick = lower if direction > 0 else upper
        # A candle closing away from the proposed direction cannot confirm it.
        if adverse_wick > 0.25:
            return None
        if (direction * (previous.close - previous.open) < 0
                and min(bar.open, bar.close) <= min(previous.open, previous.close)
                and max(bar.open, bar.close) >= max(previous.open, previous.close)):
            return "engulfing"
        if supportive_wick >= 0.50:
            return "wick_rejection"
        if body >= 0.60 and adverse_wick <= 0.20:
            return "directional_expansion"
        return None

    def evaluate(
        self,
        candles: list[Candle],
        book: OrderBook,
        trend: Trend,
        *,
        symbol: str = "",
        trades: list[TradeTick] | None = None,
        structure: MarketStructure | None = None,
        market_context: MarketContext | None = None,
        observed_at_ms: int | None = None,
        trade_flow: dict | None = None,
    ) -> StrategyDecision:
        details = {"state": "search", "beta": True, "hypothesisVersion": 1,
                   "allowRunner": False, "tradeMode": "candle_hypothesis"}

        def wait(reason: str, state: str = "search") -> StrategyDecision:
            details["state"] = state
            return StrategyDecision(self.key, Action.WAIT, [reason], details=details)

        context = market_context
        now = observed_at_ms if observed_at_ms is not None else getattr(context, "observed_at_ms", 0)
        if (context is None or not context.execution.ready
                or context.execution.book_synced is False or not now):
            return wait("BETA: ждём свежий рыночный контекст")
        closed = [c for c in candles if c.confirmed and c.start_ms + 60_000 <= now]
        if len(closed) < 21:
            return wait("BETA: нужны 21 закрытая минутная свеча")
        bar, previous = closed[-1], closed[-2]
        binding = assigned_ref(context, self.key)
        if binding is not None and binding != candle_ref(bar.start_ms):
            return wait("BETA: назначенный свечной объект заменён; нужна переоценка")
        closed_at = bar.start_ms + 60_000
        if not 0 <= now - closed_at < 60_000:
            return wait("BETA: сценарий свечи истёк")
        if self._used_bar.get(symbol, -1) >= bar.start_ms:
            return wait("BETA: сценарий этой свечи уже завершён", "found")

        htf, local = context.htf_bias, context.local_regime
        if htf is None or local is None:
            return wait("BETA: нет контекста 1h/15m/5m")
        assigned = context.scenario
        if assigned and assigned.get("owner") == self.key:
            direction = 1 if assigned["side"] == "long" else -1
        else:
            direction_trend = htf.trend_1h if htf.trend_1h != Trend.FLAT else htf.trend_15m
            if direction_trend == Trend.FLAT:
                return wait("BETA: старшие таймфреймы не задают направление")
            opposite = Trend.DOWN if direction_trend == Trend.UP else Trend.UP
            if htf.trend_15m == opposite or local.structure_5m == opposite or local.direction == opposite:
                return wait("BETA: направление 1h → 15m → 5m/1m не согласовано")
            if local.regime.value == "range":
                return wait("BETA: трендовая гипотеза не торгует боковик")
            direction = 1 if direction_trend == Trend.UP else -1
        action = Action.LONG if direction > 0 else Action.SHORT
        pattern = self._pattern(bar, previous, direction)
        if pattern is None:
            return wait("BETA: нет направленного паттерна закрытой свечи")
        baseline = median(c.volume for c in closed[-21:-1])
        volume_ratio = bar.volume / baseline if baseline > 0 else 0.0
        if volume_ratio < self.minimum_volume_ratio:
            return wait("BETA: объём паттерна ниже 1.2× медианы 20 закрытых свечей")
        flow = trade_flow if trade_flow is not None else compute_trade_flow(trades or [], now)
        if not flow.get("baselineReady"):
            return wait("BETA: ждём накопления базовой ленты сделок")
        alignment = context.flow_alignment_for(action)
        if alignment is None or alignment.classification.value not in {"aligned", "strongly_aligned"}:
            return wait("BETA: поток сделок не подтверждает направление")
        mid = book.mid
        if not mid or not book.best_bid or not book.best_ask:
            return wait("BETA: нет двустороннего стакана")
        # Geometry is fixed to the closed candle, never extended with a late fill.
        buffer = max(bar.close * 0.0001, (bar.high - bar.low) * 0.05)
        trigger = bar.high + buffer if direction > 0 else bar.low - buffer
        stop = bar.low - buffer if direction > 0 else bar.high + buffer
        risk_distance = abs(trigger - stop)
        ladder = find_liquidity_targets(closed, trigger, action,
            min_distance_pct=0.0, structure=structure)
        target, _, target_source = structural_target(trigger, action, ladder,
            movement=movement_budget(closed))
        hypothesis = {"version": 1, "pattern": pattern, "side": action.value,
                      "candleStartMs": bar.start_ms, "preparedAtMs": closed_at,
                      "expiresAtMs": closed_at + 60_000, "trigger": trigger,
                      "invalidation": stop, "target": target, "targetR": abs(target-trigger)/risk_distance if risk_distance else 0,
                      "volumeRatio": volume_ratio, "maximumChaseR": self.maximum_chase_r}
        details.update(hypothesis=hypothesis, flowAlignment=alignment.public(),
                       trendAligned=True, flowConfirmed=True,
                       targetSource=target_source, expectedImpulsePct=movement_budget(closed)/trigger,
                       freshnessHorizonSeconds=60.0,
                       preparedOpportunity={"preparedAtMs":closed_at, "source":"closed_candle_pattern"},
                       opportunityArm={"observedAtMs":closed_at, "price":trigger, "source":"closed_candle_pattern"})
        executable = book.best_ask if direction > 0 else book.best_bid
        if direction * (mid - stop) <= 0:
            self._used_bar[symbol] = bar.start_ms
            return wait("BETA: цена отменила сценарий свечи", "found")
        if direction * (mid - trigger) <= 0:
            return wait("BETA: ждём выхода цены за экстремум закрытой свечи", "armed")
        if direction * (executable - trigger) > risk_distance * self.maximum_chase_r:
            self._used_bar[symbol] = bar.start_ms
            return wait("BETA: вход запоздал более чем на 0.25R, цену не догоняем", "found")
        fire = self._fire.get(symbol)
        if fire is None or fire[0] != bar.start_ms:
            fire = (bar.start_ms, {"observedAtMs":now, "price":executable,
                                  "source":"closed_candle_extreme_break"})
            self._fire[symbol] = fire
        details.update(state="impulse", fireTrigger=dict(fire[1]))
        return StrategyDecision(self.key, action,
            [f"BETA: {pattern}, объём {volume_ratio:.2f}×; экстремум закрытой свечи подтверждён ценой"],
            watched_level=trigger, entry=executable, stop=stop, target=target,
            setup_id=f"{self.key}:{symbol}:{bar.start_ms}:{action.value}", details=details,
            visuals={"overlays":[{"type":"price", "price":trigger, "label":"BETA подтверждение"},
                                 {"type":"price", "price":stop, "label":"BETA отмена"},
                                 {"type":"price", "price":target, "label":"BETA структурная цель"}]})
