from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from time import monotonic, time

from .bybit import BybitRestClient, OrderBookSequenceError, OrderBookState, stream_symbol
from .config import Settings
from .domain import Action, Candle, Candidate, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .paper import PaperBroker, Position
from .expectancy import StrategyExpectancyBook
from .strategy_policy import minimum_expectancy_r
from .observability import build_decision_trace
from .recorder import SessionRecorder
from .risk import RiskEngine
from .strategy.flow import best_level_ofi_usd, prune_trades
from .strategy.lifecycle import LevelLifecycleTracker
from .strategy.structure import aggregate_candles
from .strategy import (
    DEFAULT_STRATEGIES,
    HTFBiasSnapshot,
    LocalRegimeSnapshot,
    MultiHorizonFlowContext,
    LiquidityEvidence,
    MarketStructure,
    Strategy,
    build_liquidity_evidence,
    build_market_structure,
    build_multi_horizon_flow_context,
    classify_context_trend,
    classify_entry_freshness,
    classify_htf_bias,
    classify_local_regime,
    compute_trade_flow,
)


ACTIVE_SETUP_STATES = {"found", "persisting", "approach", "pressure", "test", "defended", "reject", "reaction", "break", "impulse"}

ENTRY_FRESHNESS_TRIGGER_STATES = {
    "trend_structure": {"reclaim"},
    "weak_level_rejection": {"reject"},
    "level_breakout": {"break"},
}
ENTRY_FRESHNESS_RESET_STATES = {
    "search",
    "found",
    "stale_book",
    "stale_candle",
    "error",
}


@dataclass(slots=True)
class ActiveSymbolSession:
    symbol: str
    candles: list[Candle] = field(default_factory=list)
    context_5m: list[Candle] = field(default_factory=list)
    context_15m: list[Candle] = field(default_factory=list)
    context_1h: list[Candle] = field(default_factory=list)
    orderbook: OrderBook = field(default_factory=OrderBook)
    last_price: float = 0.0
    # Legacy strategy direction remains unchanged during Stage 1.
    trend: Trend = Trend.FLAT
    htf_bias: HTFBiasSnapshot | None = None
    local_regime: LocalRegimeSnapshot | None = None
    flow_context: MultiHorizonFlowContext | None = None
    liquidity_evidence: LiquidityEvidence | None = None
    market_context_fingerprint: tuple | None = None
    entry_freshness_anchors: dict[str, dict] = field(default_factory=dict)
    entry_freshness_fingerprints: dict[str, tuple] = field(default_factory=dict)
    structure: MarketStructure | None = None
    decisions: dict[str, StrategyDecision] = field(default_factory=dict)
    decision_fingerprints: dict[str, tuple] = field(default_factory=dict)
    trades: deque[TradeTick] = field(default_factory=deque)
    book_flow: deque[tuple[int, float]] = field(default_factory=deque)
    last_book_flow_ms: int = 0
    level_tracker: LevelLifecycleTracker = field(default_factory=LevelLifecycleTracker)
    consumed_setups: dict[str, str] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    nontradeable_since: dict[str, float] = field(default_factory=dict)
    activated_at: float = field(default_factory=time)
    last_ranked_at: float = field(default_factory=time)
    last_signal_at: float = 0.0
    last_trade_at: float = 0.0
    last_market_at: float = 0.0
    last_book_at: float = 0.0
    book_stale_after_seconds: float = 1.5
    confirmed_candle_stale_after_seconds: float = 150.0
    book_synced: bool | None = None
    last_trade_stream_at: float = 0.0
    last_kline_at: float = 0.0
    last_eval: float = 0.0
    last_frame: float = 0.0
    last_research_frame: float = 0.0
    last_risk_fingerprint: tuple | None = None
    last_blocked_fingerprint: tuple | None = None
    last_economic_shadow_fingerprint: tuple | None = None

    def book_age_seconds(self, now: float | None = None) -> float | None:
        if self.last_book_at <= 0:
            return None
        resolved_now = time() if now is None else now
        return max(0.0, resolved_now - self.last_book_at)

    def book_is_fresh(self, now: float | None = None) -> bool:
        age = self.book_age_seconds(now)
        if age is None:
            return False
        if self.book_synced is False:
            return False
        if not self.orderbook.bids or not self.orderbook.asks:
            return False
        return age <= self.book_stale_after_seconds

    def book_health(self, now: float | None = None) -> dict:
        return {
            "fresh": self.book_is_fresh(now),
            "synced": self.book_synced,
            "ageSeconds": self.book_age_seconds(now),
            "staleAfterSeconds": self.book_stale_after_seconds,
            "bidLevels": len(self.orderbook.bids),
            "askLevels": len(self.orderbook.asks),
        }

    def confirmed_candle_age_seconds(
        self,
        now: float | None = None,
    ) -> float | None:
        confirmed = [c for c in self.candles if c.confirmed]
        if not confirmed:
            return None
        latest = max(confirmed, key=lambda candle: candle.start_ms)
        resolved_now = time() if now is None else now
        close_at = latest.start_ms / 1000 + 60.0
        return max(0.0, resolved_now - close_at)

    def confirmed_candle_is_fresh(
        self,
        stale_after_seconds: float,
        now: float | None = None,
    ) -> bool:
        if stale_after_seconds <= 0:
            return True
        age = self.confirmed_candle_age_seconds(now)
        return age is not None and age <= stale_after_seconds

    def candle_health(
        self,
        stale_after_seconds: float,
        now: float | None = None,
    ) -> dict:
        confirmed = [c for c in self.candles if c.confirmed]
        latest = (
            max(confirmed, key=lambda candle: candle.start_ms)
            if confirmed
            else None
        )
        age = self.confirmed_candle_age_seconds(now)
        return {
            "fresh": self.confirmed_candle_is_fresh(
                stale_after_seconds,
                now,
            ),
            "ageSeconds": age,
            "staleAfterSeconds": stale_after_seconds,
            "lastConfirmedStartMs": (
                latest.start_ms if latest is not None else None
            ),
            "confirmedCount": len(confirmed),
            "totalCount": len(self.candles),
        }

    def record_book_flow(self, ts_ms: int, value: float) -> None:
        if ts_ms <= 0:
            return
        self.last_book_flow_ms = max(self.last_book_flow_ms, ts_ms)
        self.book_flow.append((ts_ms, value))
        cutoff = self.last_book_flow_ms - 60_000
        while self.book_flow and self.book_flow[0][0] < cutoff:
            self.book_flow.popleft()

    def book_flow_snapshot(self, now_ms: int | None = None) -> dict:
        resolved_now_ms = int(time() * 1000) if now_ms is None else now_ms

        def window(seconds: int) -> tuple[float, int]:
            cutoff = resolved_now_ms - seconds * 1000
            rows = [
                value
                for ts, value in self.book_flow
                if cutoff <= ts <= resolved_now_ms
            ]
            return sum(rows), len(rows)

        depth_usd = sum(
            price * qty
            for price, qty in (
                self.orderbook.bids[:5] + self.orderbook.asks[:5]
            )
        )
        ofi_5s, count_5s = window(5)
        ofi_15s, count_15s = window(15)
        ofi_60s, count_60s = window(60)
        latest_age_ms = (
            max(0, resolved_now_ms - self.last_book_flow_ms)
            if self.last_book_flow_ms > 0
            else None
        )
        return {
            "bestLevelOfiUsd5s": ofi_5s,
            "bestLevelOfiUsd15s": ofi_15s,
            "bestLevelOfiUsd60s": ofi_60s,
            "eventCount5s": count_5s,
            "eventCount15s": count_15s,
            "eventCount60s": count_60s,
            "latestEventAgeMs": latest_age_ms,
            "top5DepthUsd": depth_usd,
            "normalizedOfi5s": ofi_5s / depth_usd if depth_usd > 0 else 0.0,
            "normalizedOfi15s": ofi_15s / depth_usd if depth_usd > 0 else 0.0,
            "normalizedOfi60s": ofi_60s / depth_usd if depth_usd > 0 else 0.0,
        }

    def _trade_candles(
        self,
        bucket_seconds: int,
        now_ms: int,
    ) -> list[dict]:
        if bucket_seconds <= 0:
            return []
        buckets: dict[int, dict] = {}
        bucket_ms = bucket_seconds * 1000
        for trade in self.trades:
            bucket_start = trade.ts_ms // bucket_ms * bucket_ms
            row = buckets.get(bucket_start)
            if row is None:
                row = {
                    "time": bucket_start // 1000,
                    "open": trade.price,
                    "high": trade.price,
                    "low": trade.price,
                    "close": trade.price,
                    "volume": 0.0,
                    "turnover": 0.0,
                    "confirmed": (
                        bucket_start + bucket_ms <= now_ms
                    ),
                }
                buckets[bucket_start] = row
            row["high"] = max(row["high"], trade.price)
            row["low"] = min(row["low"], trade.price)
            row["close"] = trade.price
            row["volume"] += trade.size
            row["turnover"] += trade.notional
            row["confirmed"] = (
                bucket_start + bucket_ms <= now_ms
            )
        return [
            buckets[key]
            for key in sorted(buckets)
        ]

    def _chart_context_with_live_bucket(
        self,
        closed_context: list[Candle],
        interval_minutes: int,
        limit: int,
        now_ms: int,
    ) -> list[dict]:
        """Return closed REST history plus the current live bucket.

        Higher-timeframe REST context intentionally stores only confirmed
        candles for strategy logic. The chart, however, should still show the
        candle that is currently forming. Build that last bucket from the live
        1m stream without feeding it back into strategy context.
        """
        rows = list(closed_context[-limit:])
        if not self.candles:
            return [row.public() for row in rows]

        # Only the latest aggregate bucket is needed. Two intervals of 1m
        # history are enough to reconstruct it while keeping this cheap on the
        # state endpoint.
        source_size = max(interval_minutes * 2, 120)
        live_rows = aggregate_candles(
            self.candles[-source_size:],
            interval_minutes,
        )
        if not live_rows:
            return [row.public() for row in rows]

        live = live_rows[-1]
        live.confirmed = (
            live.start_ms + interval_minutes * 60_000 <= now_ms
        )
        if not rows or live.start_ms > rows[-1].start_ms:
            rows.append(live)

        return [row.public() for row in rows[-limit:]]

    def chart_series(self, now_ms: int | None = None) -> dict:
        resolved_now = (
            int(time() * 1000)
            if now_ms is None
            else now_ms
        )
        ten_minute = aggregate_candles(
            self.candles[-720:],
            10,
        )
        for row in ten_minute:
            row.confirmed = row.start_ms + 10 * 60_000 <= resolved_now
        return {
            "5s": self._trade_candles(5, resolved_now),
            "15s": self._trade_candles(15, resolved_now),
            "1m": [x.public() for x in self.candles[-720:]],
            "5m": self._chart_context_with_live_bucket(
                self.context_5m,
                5,
                576,
                resolved_now,
            ),
            "10m": [x.public() for x in ten_minute[-72:]],
            "15m": self._chart_context_with_live_bucket(
                self.context_15m,
                15,
                480,
                resolved_now,
            ),
            "1h": self._chart_context_with_live_bucket(
                self.context_1h,
                60,
                336,
                resolved_now,
            ),
        }

    def density_context(
        self,
        now_ms: int | None = None,
    ) -> dict | None:
        decision = self.decisions.get("orderbook_density")
        if decision is None:
            return None
        details = decision.details or {}
        wall_price = details.get("wallPrice")
        if wall_price is None:
            return None
        wall_price = float(wall_price)
        wall_side = str(details.get("wallSide") or "")
        rows = (
            self.orderbook.bids
            if wall_side == "bid"
            else self.orderbook.asks
        )
        nearest_index = None
        if rows:
            nearest_index = min(
                range(len(rows)),
                key=lambda index: abs(rows[index][0] - wall_price),
            )
        focus: list[list[float]] = []
        if nearest_index is not None:
            start = max(0, nearest_index - 6)
            end = min(len(rows), nearest_index + 7)
            focus = [
                [price, qty, price * qty]
                for price, qty in rows[start:end]
            ]
        resolved_now = (
            int(time() * 1000)
            if now_ms is None
            else now_ms
        )
        return {
            "state": details.get("state"),
            "wallSide": wall_side,
            "wallPrice": wall_price,
            "notionalUsd": details.get("notionalUsd"),
            "strengthMultiple": details.get("strengthMultiple"),
            "requiredStrengthMultiple": details.get(
                "requiredStrengthMultiple"
            ),
            "remainingRatio": details.get("remainingRatio"),
            "attackNotional5s": details.get("attackNotional5s"),
            "attackRatio": details.get("attackRatio"),
            "depletionPerSecond": details.get(
                "depletionPerSecond"
            ),
            "replenishmentRatio": details.get(
                "replenishmentRatio"
            ),
            "absorptionObserved": details.get(
                "absorptionObserved"
            ),
            "wallPresent": details.get("wallPresent"),
            "distancePct": details.get("distancePct"),
            "levelFlow": details.get("levelFlow"),
            "recentLevelFlow": details.get(
                "recentLevelFlow"
            ),
            "positionInvalidated": details.get(
                "positionInvalidated"
            ),
            "bookFlow": self.book_flow_snapshot(resolved_now),
            "focusLevels": focus,
        }

    def market_context_public(self) -> dict:
        return {
            "legacyTrend": self.trend.value,
            "htfBias": (
                self.htf_bias.public()
                if self.htf_bias is not None
                else None
            ),
            "localRegime": (
                self.local_regime.public()
                if self.local_regime is not None
                else None
            ),
            "flowContext": (
                self.flow_context.public()
                if self.flow_context is not None
                else None
            ),
            "liquidityEvidence": (
                self.liquidity_evidence.public()
                if self.liquidity_evidence is not None
                else None
            ),
        }

    def market_snapshot(self) -> dict:
        now_ms = int(time() * 1000)
        return {
            "symbol": self.symbol,
            "lastPrice": self.last_price,
            "trend": self.trend.value,
            "marketContext": self.market_context_public(),
            "candles": [x.public() for x in self.candles[-240:]],
            "chartSeries": self.chart_series(now_ms),
            "orderbook": self.orderbook.public(50),
            "densityContext": self.density_context(now_ms),
            "bookHealth": self.book_health(),
            "candleHealth": self.candle_health(
                self.confirmed_candle_stale_after_seconds
            ),
            "tradeFlow": compute_trade_flow(list(self.trades), now_ms),
            "bookFlow": self.book_flow_snapshot(now_ms),
            "tradeBufferSeconds": (
                (self.trades[-1].ts_ms - self.trades[0].ts_ms) / 1000
                if len(self.trades) >= 2 else 0.0
            ),
            "recentTrades": [trade.public() for trade in list(self.trades)[-20:]],
            "structure": self.structure.public() if self.structure else None,
            "decisions": {
                key: {
                    **decision.public(),
                    "trace": build_decision_trace(
                        decision,
                        self.trend,
                        now_ms,
                        market_context=self.market_context_public(),
                    ),
                }
                for key, decision in self.decisions.items()
            },
        }

    def frame(
        self,
        book_depth: int,
        position: dict | None,
        recent_trade_limit: int = 250,
    ) -> dict:
        now_ms = int(time() * 1000)
        return {
            "lastPrice": self.last_price,
            "trend": self.trend.value,
            "marketContext": self.market_context_public(),
            "candle": self.candles[-1].public() if self.candles else None,
            "orderbook": self.orderbook.public(book_depth),
            "bookHealth": self.book_health(),
            "candleHealth": self.candle_health(
                self.confirmed_candle_stale_after_seconds
            ),
            "tradeFlow": compute_trade_flow(list(self.trades), now_ms),
            "bookFlow": self.book_flow_snapshot(now_ms),
            "position": position,
            "recentTrades": [
                trade.public()
                for trade in (
                    list(self.trades)[-max(0, recent_trade_limit):]
                    if recent_trade_limit > 0
                    else []
                )
            ],
            "structure": self.structure.public() if self.structure else None,
        }


@dataclass(slots=True)
class Opportunity:
    score: float
    session: ActiveSymbolSession
    decision: StrategyDecision
    plan: object


class TradingEngine:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.rest = BybitRestClient(config)
        self.risk = RiskEngine(config)
        self.broker = PaperBroker(config)
        self.recorder = SessionRecorder(config.session_dir)
        self.strategies: dict[str, Strategy] = {x.key: x for x in DEFAULT_STRATEGIES}
        configured_strategy_state = {
            "trend_structure": config.trend_structure_enabled,
            "weak_level_rejection": config.weak_level_rejection_enabled,
            "orderbook_density": config.density_enabled,
            "level_breakout": config.breakout_enabled,
        }
        self.strategy_enabled: dict[str, bool] = {
            key: bool(configured_strategy_state.get(key, True))
            for key in self.strategies
        }
        self.expectancy = StrategyExpectancyBook(
            list(self.strategies)
        )
        self.strategy_stats: dict[str, dict[str, object]] = {
            x.key: {
                "decisions": 0,
                "decisionUpdates": 0,
                "tradeableSignals": 0,
                "uniqueTradeableSetups": 0,
                "waitDecisions": 0,
                "riskRejects": 0,
                "uniqueRiskRejectedSetups": 0,
                "tradesOpened": 0,
                "tradesClosed": 0,
                "wins": 0,
                "losses": 0,
                "netPnl": 0.0,
                "stateCounts": {},
            }
            for x in DEFAULT_STRATEGIES
        }
        density_strategy = self.strategies.get("orderbook_density")
        if density_strategy is not None:
            setattr(
                density_strategy,
                "min_wall_notional_usd",
                config.density_min_wall_notional_usd,
            )
            setattr(
                density_strategy,
                "strength_multiple",
                config.density_strength_multiple,
            )
            setattr(
                density_strategy,
                "turnover_floor_fraction",
                config.density_turnover_floor_fraction,
            )
            setattr(
                density_strategy,
                "neighbor_window_levels",
                config.density_neighbor_window_levels,
            )
            setattr(
                density_strategy,
                "max_distance_pct",
                config.density_max_distance_pct,
            )
        self._seen_tradeable_setups: dict[str, set[tuple[str, str]]] = {
            key: set()
            for key in self.strategies
        }
        self._seen_risk_rejected_setups: dict[
            str,
            set[tuple[str, str]],
        ] = {
            key: set()
            for key in self.strategies
        }
        self.running = False
        self.candidates: list[Candidate] = []
        self.sessions: dict[str, ActiveSymbolSession] = {}
        self.events: deque[dict] = deque(maxlen=260)
        self._tasks: list[asyncio.Task] = []
        self._worker_tasks: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self._stop = asyncio.Event()
        self._paper_timer_task: asyncio.Task | None = None
        self._run_started_at: float | None = None
        self._run_deadline_at: float | None = None
        self._last_run_summary: dict | None = None
        self._scanner_error: str | None = None
        self._last_scan_ok_at: float | None = None
        self._last_scan_error_at: float | None = None

    async def start(self) -> None:
        self._stop.clear()
        try:
            await self._scan_once()
        except Exception as exc:
            self._record_scanner_error("startup_scan_error", exc)
        self._tasks = [
            asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._context_loop(), name="context"),
            asyncio.create_task(self._arbiter_loop(), name="trade-arbiter"),
        ]

    async def close(self) -> None:
        if self.running:
            self._stop_trading("shutdown")
        else:
            self._cancel_all_pending("shutdown")
            self._close_all_positions("shutdown")
        self._cancel_run_timer()
        self._stop.set()
        for task, stop_event in self._worker_tasks.values():
            stop_event.set()
            task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(
            *(x[0] for x in self._worker_tasks.values()),
            *self._tasks,
            return_exceptions=True,
        )
        await self.rest.close()

    def set_running(self, value: bool) -> None:
        if value:
            if self.running:
                return
            blocked = self.start_block_reason()
            if blocked:
                raise RuntimeError(
                    f"cannot start paper run: {blocked}"
                )
            now = time()
            self.running = True
            self._run_started_at = now
            self._run_deadline_at = now + self.config.paper_run_duration_seconds
            self._last_run_summary = None
            self._cancel_run_timer()
            self._paper_timer_task = asyncio.create_task(
                self._paper_run_timer(),
                name="paper-run-timer",
            )
            self._emit(
                "bot_started",
                None,
                {
                    "runLabel": self.config.run_label,
                    "startedAt": self._run_started_at,
                    "deadlineAt": self._run_deadline_at,
                    "durationSeconds": self.config.paper_run_duration_seconds,
                    "config": self._run_config_snapshot(),
                },
            )
            return

        self._stop_trading("bot_stop")

    async def _paper_run_timer(self) -> None:
        try:
            await asyncio.sleep(self.config.paper_run_duration_seconds)
        except asyncio.CancelledError:
            raise
        if self.running:
            self._stop_trading("duration_elapsed", cancel_timer=False)

    def _stop_trading(self, reason: str, *, cancel_timer: bool = True) -> None:
        if (
            not self.running
            and not self.broker.positions
            and not self.broker.pending_entries
        ):
            return
        stopped_at = time()
        self.running = False
        if cancel_timer:
            self._cancel_run_timer()
        self._cancel_all_pending(reason)
        self._close_all_positions(reason)
        started_at = self._run_started_at
        summary = {
            "runLabel": self.config.run_label,
            "reason": reason,
            "startedAt": started_at,
            "stoppedAt": stopped_at,
            "elapsedSeconds": max(0.0, stopped_at - started_at) if started_at else 0.0,
            "configuredDurationSeconds": self.config.paper_run_duration_seconds,
            "balance": self.broker.balance,
            "realizedPnl": self.broker.total_pnl,
            "closedTrades": self.broker.total_closed_trades,
        }
        self._last_run_summary = summary
        self._emit("run_summary", None, summary)
        self._emit("bot_stopped", None, {"reason": reason})
        self._run_deadline_at = None

    def _cancel_run_timer(self) -> None:
        task = self._paper_timer_task
        self._paper_timer_task = None
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    def _run_config_snapshot(self) -> dict:
        return {
            "startBalance": self.config.start_balance,
            "durationSeconds": self.config.paper_run_duration_seconds,
            "minTurnoverUsd": self.config.min_turnover_usd,
            "workingSymbols": self.config.working_symbols,
            "maxActiveSymbols": self.config.max_active_symbols,
            "minNetProfitUsd": self.config.min_net_profit_usd,
            "minNetProfitEquityFraction": self.config.min_net_profit_equity_fraction,
            "minNetRewardRisk": self.config.min_net_reward_risk,
            "enforceNetRewardRiskGate": self.config.enforce_net_reward_risk_gate,
            "riskFraction": self.config.risk_fraction,
            "maxTradeAllInLossFraction": self.config.max_trade_all_in_loss_fraction,
            "maxTotalRiskFraction": self.config.max_total_risk_fraction,
            "maxLeverage": self.config.max_leverage,
            "enabledStrategies": [
                key
                for key, enabled in self.strategy_enabled.items()
                if enabled
            ],
            "evidenceOnlyStrategies": ["orderbook_density"],
            "confirmedCandleStaleSeconds": (
                self.config.confirmed_candle_stale_seconds
            ),
            "passiveEntryEnabled": self.config.passive_entry_enabled,
            "passiveEntryTimeoutSeconds": self.config.passive_entry_timeout_seconds,
            "makerFillConfirmationBps": self.config.maker_fill_confirmation_bps,
            "takerFeeRate": self.config.taker_fee_rate,
            "makerFeeRate": self.config.maker_fee_rate,
            "slippageBps": self.config.slippage_bps,
            "maxWinnerCostShare": self.config.max_winner_cost_share,
            "enforceWinnerCostShareGate": self.config.enforce_winner_cost_share_gate,
            "minFirstTakeMovePct": self.config.min_first_take_move_pct,
            "enforceMinFirstTakeMoveGate": self.config.enforce_min_first_take_move_gate,
            "partialTakeAtR": self.config.partial_take_at_r,
            "partialTakeFraction": self.config.partial_take_fraction,
            "runnerTargetR": self.config.runner_target_r,
            "noFollowThroughSeconds": self.config.no_follow_through_seconds,
            "replayEngagedFrameSeconds": self.config.replay_engaged_frame_seconds,
            "replayIdleFrameSeconds": self.config.replay_idle_frame_seconds,
        }

    def toggle_strategy(self, key: str, enabled: bool) -> None:
        if key not in self.strategy_enabled:
            raise KeyError(key)
        self.strategy_enabled[key] = enabled
        self._emit("strategy_toggle", None, {"strategy": key, "enabled": enabled})

    async def _scanner_loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = (
                    self.config.scanner_interval_seconds
                    if self.sessions
                    else min(
                        self.config.scanner_interval_seconds,
                        self.config.empty_startup_rescan_seconds,
                    )
                )
                await asyncio.sleep(max(0.1, delay))
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_scanner_error("scanner_error", exc)

    def _record_scanner_error(
        self,
        event: str,
        exc: Exception,
    ) -> None:
        self._scanner_error = f"{type(exc).__name__}: {exc}"
        self._last_scan_error_at = time()
        self._emit(event, None, {"error": self._scanner_error})

    def market_health(self) -> dict:
        now = time()
        live_sessions = [
            session
            for session in self.sessions.values()
            if session.last_market_at > 0
            and now - session.last_market_at <= self.config.market_stale_seconds
            and session.book_is_fresh(now)
            and session.confirmed_candle_is_fresh(
                self.config.confirmed_candle_stale_seconds,
                now,
            )
        ]
        ready = bool(self.candidates) and bool(live_sessions)
        if self._scanner_error:
            reason = self._scanner_error
        elif not self.candidates:
            reason = "scanner has no eligible candidates"
        elif not self.sessions:
            reason = "no active symbol sessions were bootstrapped"
        elif not live_sessions:
            stale_candles = [
                session.symbol
                for session in self.sessions.values()
                if not session.confirmed_candle_is_fresh(
                    self.config.confirmed_candle_stale_seconds,
                    now,
                )
            ]
            reason = (
                "confirmed 1m candle history is stale: "
                + ", ".join(stale_candles[:6])
                if stale_candles
                else "waiting for fresh synchronized websocket market data"
            )
        else:
            reason = None
        return {
            "ready": ready,
            "reason": reason,
            "scannerError": self._scanner_error,
            "lastScanOkAt": self._last_scan_ok_at,
            "lastScanErrorAt": self._last_scan_error_at,
            "candidateCount": len(self.candidates),
            "activeSymbolCount": len(self.sessions),
            "liveSymbolCount": len(live_sessions),
        }

    def start_block_reason(self) -> str | None:
        health = self.market_health()
        return None if health["ready"] else str(
            health["reason"] or "market data is not ready"
        )

    async def _scan_once(self) -> None:
        candidates = await self.rest.active_candidates()
        if not candidates:
            raise RuntimeError(
                "scanner returned zero eligible candidates"
            )
        self.candidates = candidates
        self._scanner_error = None
        self._last_scan_ok_at = time()
        now = time()
        candidate_map = {item.symbol: item for item in self.candidates}

        for symbol, session in self.sessions.items():
            candidate = candidate_map.get(symbol)
            if candidate and (candidate.activity_rank or 999) <= self.config.active_keep_rank:
                session.last_ranked_at = now

        for candidate in self.candidates[: self.config.working_symbols]:
            await self._promote_symbol(candidate.symbol, now)

        await self._cleanup_active_symbols(now)
        self._emit(
            "scanner_update",
            None,
            {
                "active": list(self.sessions),
                "promotedFromTop": [x.symbol for x in self.candidates[: self.config.working_symbols]],
                # Persist the complete candidate profile so post-run
                # reports can explain why a coin entered or left the
                # working universe without relying on live API state.
                "ranked": [x.public() for x in self.candidates],
            },
        )

    async def _promote_symbol(self, symbol: str, now: float) -> None:
        if symbol in self.sessions:
            self.sessions[symbol].last_ranked_at = now
            return

        if len(self.sessions) >= self.config.max_active_symbols:
            evictable = [
                session
                for session in self.sessions.values()
                if self._can_deactivate(session, now)
            ]
            if not evictable:
                return
            victim = min(evictable, key=lambda x: x.last_ranked_at)
            self._deactivate_symbol(victim.symbol, "capacity_rotation")

        try:
            await self._bootstrap_symbol(symbol)
        except Exception as exc:
            self._emit(
                "symbol_bootstrap_error",
                symbol,
                {"error": str(exc)},
            )
            return
        self.sessions[symbol].last_ranked_at = now
        stop_event = asyncio.Event()
        task = asyncio.create_task(self._symbol_worker(symbol, stop_event), name=f"market-{symbol}")
        self._worker_tasks[symbol] = (task, stop_event)

    async def _cleanup_active_symbols(self, now: float) -> None:
        for symbol, session in list(self.sessions.items()):
            if self._can_deactivate(session, now):
                self._deactivate_symbol(symbol, "idle_after_active_window")

    def _can_deactivate(self, session: ActiveSymbolSession, now: float) -> bool:
        if session.symbol in self.broker.positions:
            return False
        if session.symbol in self.broker.pending_entries:
            return False
        if now - session.activated_at < self.config.active_symbol_min_seconds:
            return False
        if now - session.last_ranked_at < self.config.active_symbol_idle_timeout_seconds:
            return False
        if self._session_engaged(session):
            return False
        return True

    def _session_engaged(self, session: ActiveSymbolSession) -> bool:
        if any(decision.tradeable for decision in session.decisions.values()):
            return True
        for decision in session.decisions.values():
            state = str(decision.details.get("state") or "")
            if state in ACTIVE_SETUP_STATES:
                return True
            if decision.watched_level is not None and decision.confidence >= 0.5:
                return True
        return False

    def _research_book_depth(
        self,
        session: ActiveSymbolSession,
        position: Position | None,
    ) -> int:
        density = session.decisions.get("orderbook_density")
        density_state = (
            str(density.details.get("state") or "")
            if density is not None
            else ""
        )
        density_engaged = (
            density_state in ACTIVE_SETUP_STATES
            or (
                position is not None
                and position.strategy == "orderbook_density"
            )
        )
        if density_engaged:
            return self.config.orderbook_depth
        return min(self.config.orderbook_depth, 50)

    def _deactivate_symbol(self, symbol: str, reason: str) -> None:
        worker = self._worker_tasks.pop(symbol, None)
        if worker:
            task, stop_event = worker
            stop_event.set()
            task.cancel()
        self._emit("symbol_deactivated", symbol, {"reason": reason})
        for strategy in self.strategies.values():
            strategy.reset(symbol)
        self.sessions.pop(symbol, None)

    async def _bootstrap_symbol(self, symbol: str) -> None:
        candles, context_5m, context_15m, context_1h = await asyncio.gather(
            self.rest.klines(
                symbol,
                "1",
                self.config.bootstrap_1m_candles,
            ),
            self.rest.klines(
                symbol,
                "5",
                self.config.bootstrap_5m_candles,
            ),
            self.rest.klines(
                symbol,
                "15",
                self.config.bootstrap_15m_candles,
            ),
            self.rest.klines(
                symbol,
                "60",
                self.config.bootstrap_1h_candles,
            ),
        )
        now = time()
        session = ActiveSymbolSession(
            symbol=symbol,
            candles=candles,
            context_5m=[x for x in context_5m if x.confirmed],
            context_15m=[x for x in context_15m if x.confirmed],
            context_1h=[x for x in context_1h if x.confirmed],
            book_stale_after_seconds=self.config.book_stale_seconds,
            confirmed_candle_stale_after_seconds=(
                self.config.confirmed_candle_stale_seconds
            ),
            activated_at=now,
            last_ranked_at=now,
        )
        session.last_price = candles[-1].close if candles else 0
        closed_1m = [x for x in session.candles if x.confirmed]
        session.trend = classify_context_trend(
            session.context_15m,
            session.context_1h,
        )
        session.htf_bias = classify_htf_bias(
            session.context_15m,
            session.context_1h,
        )
        session.local_regime = classify_local_regime(
            closed_1m,
            session.context_5m,
        )
        session.market_context_fingerprint = self._market_context_fingerprint(
            session
        )
        self.sessions[symbol] = session
        self._emit(
            "symbol_activated",
            symbol,
            {
                "market": session.market_snapshot(),
                "reason": "promoted from liquid activity ranking",
            },
        )

    async def _context_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60)
                items = list(self.sessions.items())
                if not items:
                    continue
                async def refresh(
                    symbol: str,
                    session: ActiveSymbolSession,
                ):
                    stale_1m = not session.confirmed_candle_is_fresh(
                        self.config.confirmed_candle_stale_seconds,
                    )
                    one_minute = (
                        self.rest.klines(
                            symbol,
                            "1",
                            min(self.config.bootstrap_1m_candles, 240),
                        )
                        if stale_1m
                        else asyncio.sleep(0, result=None)
                    )
                    return await asyncio.gather(
                        one_minute,
                        self.rest.klines(
                            symbol,
                            "5",
                            self.config.bootstrap_5m_candles,
                        ),
                        self.rest.klines(
                            symbol,
                            "15",
                            self.config.bootstrap_15m_candles,
                        ),
                        self.rest.klines(
                            symbol,
                            "60",
                            self.config.bootstrap_1h_candles,
                        ),
                    )

                results = await asyncio.gather(
                    *(
                        refresh(symbol, session)
                        for symbol, session in items
                    ),
                    return_exceptions=True,
                )
                for (symbol, session), result in zip(
                    items,
                    results,
                    strict=True,
                ):
                    if isinstance(result, Exception):
                        continue
                    (
                        refreshed_1m,
                        context_5m,
                        context_15m,
                        context_1h,
                    ) = result
                    if refreshed_1m:
                        age_before = session.confirmed_candle_age_seconds()
                        by_start = {
                            candle.start_ms: candle
                            for candle in session.candles
                        }
                        for candle in refreshed_1m:
                            previous = by_start.get(candle.start_ms)
                            if (
                                previous is not None
                                and previous.confirmed
                            ):
                                candle.confirmed = True
                            by_start[candle.start_ms] = candle
                        session.candles = sorted(
                            by_start.values(),
                            key=lambda candle: candle.start_ms,
                        )[-self.config.bootstrap_1m_candles:]
                        age_after = session.confirmed_candle_age_seconds()
                        self._emit(
                            "candle_resync",
                            symbol,
                            {
                                "reason": "stale_confirmed_1m",
                                "ageBeforeSeconds": age_before,
                                "ageAfterSeconds": age_after,
                                "fetchedCandles": len(refreshed_1m),
                            },
                        )
                    session.context_5m = [x for x in context_5m if x.confirmed]
                    session.context_15m = [x for x in context_15m if x.confirmed]
                    session.context_1h = [x for x in context_1h if x.confirmed]
                    self._refresh_market_context(
                        session,
                        closed_1m=[
                            x for x in session.candles
                            if x.confirmed
                        ],
                        closed_5m=session.context_5m,
                        closed_15m=session.context_15m,
                        closed_1h=session.context_1h,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("context_error", None, {"error": str(exc)})

    async def _symbol_worker(self, symbol: str, stop_event: asyncio.Event) -> None:
        book_state = OrderBookState(self.config.orderbook_depth)

        async def on_message(message: dict) -> None:
            session = self.sessions.get(symbol)
            if session is None:
                return

            wall_now = time()
            session.last_market_at = wall_now
            topic = message.get("topic", "")
            if topic.startswith("orderbook."):
                previous_book = session.orderbook
                try:
                    session.orderbook = book_state.apply(message)
                except OrderBookSequenceError:
                    session.last_book_at = 0.0
                    session.book_synced = False
                    session.orderbook = OrderBook()
                    raise
                session.book_synced = book_state.synced
                session.last_book_at = wall_now
                data = message.get("data") or {}
                if (
                    message.get("type") != "snapshot"
                    and int(data.get("u") or 0) != 1
                    and previous_book.bids
                    and previous_book.asks
                ):
                    event_ms = int(
                        message.get("cts")
                        or message.get("ts")
                        or wall_now * 1000
                    )
                    session.record_book_flow(
                        event_ms,
                        best_level_ofi_usd(
                            previous_book,
                            session.orderbook,
                        ),
                    )
                self._mark_position_from_book(session)
            elif topic.startswith("kline."):
                self._apply_kline(session, message)
                session.last_kline_at = wall_now
            elif topic.startswith("publicTrade."):
                rows = message.get("data") or []
                if rows:
                    for row in rows:
                        session.trades.append(
                            TradeTick(
                                ts_ms=int(row.get("T") or time() * 1000),
                                price=float(row["p"]),
                                size=float(row["v"]),
                                side=str(row.get("S") or ""),
                            )
                        )
                    session.last_price = float(rows[-1]["p"])
                    session.last_trade_stream_at = wall_now
                    prune_trades(
                        session.trades,
                        int(rows[-1].get("T") or time() * 1000),
                        self.config.trade_buffer_seconds,
                    )
                    self._mark_execution_from_market(
                        session,
                        trade_ts_ms=int(
                            rows[-1].get("T") or time() * 1000
                        ),
                    )

            now = monotonic()
            if now - session.last_eval >= 0.8:
                session.last_eval = now
                await self._evaluate(session)

            position = self.broker.positions.get(symbol)

            if (
                now - session.last_research_frame
                >= self.config.research_frame_seconds
            ):
                session.last_research_frame = now
                self.recorder.record(
                    "research_frame",
                    symbol,
                    session.frame(
                        self._research_book_depth(
                            session,
                            position,
                        ),
                        position.public() if position else None,
                        self.config.research_recent_trades,
                    ),
                )

            frame_interval = (
                self.config.replay_engaged_frame_seconds
                if position is not None or self._session_engaged(session)
                else self.config.replay_idle_frame_seconds
            )
            if now - session.last_frame >= frame_interval:
                session.last_frame = now
                self.recorder.record(
                    "market_frame",
                    symbol,
                    session.frame(
                        self.config.replay_book_depth,
                        position.public() if position else None,
                        self.config.replay_recent_trades,
                    ),
                )

        await stream_symbol(
            self.config.bybit_public_ws_url,
            symbol,
            on_message,
            stop_event,
            self.config.orderbook_depth,
        )

    def _apply_kline(
        self,
        session: ActiveSymbolSession,
        message: dict,
    ) -> None:
        rows = message.get("data") or []
        if not rows:
            return

        incoming: list[Candle] = []
        for row in rows:
            raw_confirm = row.get("confirm")
            confirmed = (
                raw_confirm is True
                or str(raw_confirm).lower() == "true"
            )
            incoming.append(
                Candle(
                    start_ms=int(row["start"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    turnover=float(row["turnover"]),
                    confirmed=confirmed,
                )
            )

        incoming.sort(key=lambda candle: candle.start_ms)
        by_start = {
            candle.start_ms: candle
            for candle in session.candles
        }
        for candle in incoming:
            previous = by_start.get(candle.start_ms)
            if previous is not None and previous.confirmed:
                candle.confirmed = True
            by_start[candle.start_ms] = candle

        # Bybit can send the just-closed candle and the new forming candle in
        # one boundary update. It can also send only the new bucket. Either
        # way, once a newer minute exists, every older forming 1m bucket is
        # definitively closed and must never remain unconfirmed forever.
        newest_start = max(candle.start_ms for candle in incoming)
        for candle in by_start.values():
            if candle.start_ms < newest_start and not candle.confirmed:
                candle.confirmed = True

        session.candles = sorted(
            by_start.values(),
            key=lambda candle: candle.start_ms,
        )[-self.config.bootstrap_1m_candles:]
        latest = max(incoming, key=lambda candle: candle.start_ms)
        session.last_price = latest.close

    async def _evaluate(self, session: ActiveSymbolSession) -> None:
        if not session.candles:
            return

        closed_1m = [x for x in session.candles if x.confirmed]
        closed_5m = [x for x in session.context_5m if x.confirmed]
        closed_15m = [x for x in session.context_15m if x.confirmed]
        closed_1h = [x for x in session.context_1h if x.confirmed]
        if not closed_1m:
            return
        if not session.confirmed_candle_is_fresh(
            self.config.confirmed_candle_stale_seconds,
        ):
            age = session.confirmed_candle_age_seconds()
            for key in self.strategies:
                if not self.strategy_enabled.get(key, False):
                    continue
                decision = StrategyDecision(
                    strategy=key,
                    action=Action.WAIT,
                    reasons=[
                        "Подтверждённая 1m история устарела; торговля запрещена до восстановления свечного потока"
                    ],
                    details={
                        "state": "stale_candle",
                        "confirmedCandleAgeSeconds": age,
                        "staleAfterSeconds": (
                            self.config.confirmed_candle_stale_seconds
                        ),
                    },
                )
                session.decisions[key] = decision
                self._record_decision_if_changed(session, decision)
            return
        self._refresh_market_context(
            session,
            closed_1m=closed_1m,
            closed_5m=closed_5m,
            closed_15m=closed_15m,
            closed_1h=closed_1h,
        )
        reference_price = session.orderbook.mid or session.last_price
        session.structure = build_market_structure(
            closed_1m,
            closed_15m,
            reference_price,
            context_5m=closed_5m,
            context_1h=closed_1h,
        )
        session.structure = session.level_tracker.update(
            session.structure,
            closed_1m,
            reference_price,
            int(time() * 1000),
        )
        now = time()
        now_ms = int(now * 1000)
        trade_flow = compute_trade_flow(
            list(session.trades),
            now_ms,
        )
        book_flow = session.book_flow_snapshot(now_ms)
        session.flow_context = build_multi_horizon_flow_context(
            trade_flow,
            book_flow,
            observed_at_ms=now_ms,
        )
        book_fresh = session.book_is_fresh(now)
        if not self.strategy_enabled.get("orderbook_density", False):
            session.liquidity_evidence = None

        strategy_items = list(self.strategies.items())
        strategy_items.sort(
            key=lambda item: 0 if item[0] == "orderbook_density" else 1
        )
        for key, strategy in strategy_items:
            if not self.strategy_enabled[key]:
                continue
            if key == "orderbook_density" and not book_fresh:
                decision = StrategyDecision(
                    strategy=key,
                    action=Action.WAIT,
                    reasons=[
                        "Стакан не синхронизирован или устарел; liquidity evidence не обновляется"
                    ],
                    details={
                        "state": "stale_book",
                        "bookHealth": session.book_health(now),
                        "positionInvalidated": False,
                        "evidenceOnly": True,
                    },
                )
                session.liquidity_evidence = build_liquidity_evidence(
                    decision
                )
                decision.details["liquidityEvidence"] = (
                    session.liquidity_evidence.public()
                )
                session.decisions[key] = decision
                self._record_decision_if_changed(session, decision)
                continue
            try:
                decision = strategy.evaluate(
                    closed_1m,
                    session.orderbook,
                    session.trend,
                    symbol=session.symbol,
                    trades=list(session.trades),
                    structure=session.structure,
                    observed_at_ms=int(now * 1000),
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                previous = session.decisions.get(key)
                if previous is None or previous.details.get("error") != error:
                    self._emit(
                        "strategy_error",
                        session.symbol,
                        {"strategy": key, "error": error},
                        snapshot=True,
                    )
                decision = StrategyDecision(
                    strategy=key,
                    action=Action.WAIT,
                    reasons=[f"Ошибка стратегии: {error}"],
                    details={"state": "error", "error": error},
                )
            self._annotate_flow_context(
                session,
                decision,
            )

            if key == "orderbook_density":
                session.liquidity_evidence = build_liquidity_evidence(
                    decision
                )
                decision.details["liquidityEvidence"] = (
                    session.liquidity_evidence.public()
                )
                decision = self._density_as_evidence_only(decision)
                session.decisions[key] = decision
                self._record_decision_if_changed(session, decision)
                continue

            self._annotate_liquidity_evidence(
                session,
                decision,
            )
            self._annotate_entry_freshness(
                session,
                decision,
                observed_at=now,
            )
            if decision.tradeable:
                decision.setup_id = self._resolve_setup_id(session, decision)
                session.last_signal_at = now
                session.nontradeable_since.pop(key, None)
            else:
                self._observe_wait_for_rearm(session, key, now)

            session.decisions[key] = decision
            self._record_decision_if_changed(session, decision)

        self._maybe_strategy_invalidation(session)

    @staticmethod
    def _market_context_fingerprint(
        session: ActiveSymbolSession,
    ) -> tuple:
        htf = session.htf_bias
        local = session.local_regime
        return (
            session.trend.value,
            htf.bias.value if htf is not None else None,
            htf.alignment if htf is not None else None,
            local.regime.value if local is not None else None,
            local.direction.value if local is not None else None,
            local.parent_direction.value if local is not None else None,
        )

    def _refresh_market_context(
        self,
        session: ActiveSymbolSession,
        *,
        closed_1m: list[Candle],
        closed_5m: list[Candle],
        closed_15m: list[Candle],
        closed_1h: list[Candle],
    ) -> None:
        session.trend = classify_context_trend(
            closed_15m,
            closed_1h,
        )
        session.htf_bias = classify_htf_bias(
            closed_15m,
            closed_1h,
        )
        session.local_regime = classify_local_regime(
            closed_1m,
            closed_5m,
        )
        fingerprint = self._market_context_fingerprint(session)
        if session.market_context_fingerprint == fingerprint:
            return
        previous = session.market_context_fingerprint
        session.market_context_fingerprint = fingerprint
        self._emit(
            "market_context_changed",
            session.symbol,
            {
                "previousFingerprint": list(previous) if previous else None,
                "marketContext": session.market_context_public(),
            },
        )

    @staticmethod
    def _density_as_evidence_only(
        decision: StrategyDecision,
    ) -> StrategyDecision:
        details = dict(decision.details or {})
        details["evidenceOnly"] = True
        if decision.action in {Action.LONG, Action.SHORT}:
            details["shadowAction"] = decision.action.value
            details["shadowEntry"] = decision.entry
            details["shadowStop"] = decision.stop
            details["shadowTarget"] = decision.target
            details["shadowConfidence"] = decision.confidence
            reasons = [
                *decision.reasons,
                "Density переведена в evidence-only: самостоятельный вход отключён",
            ]
        else:
            reasons = list(decision.reasons)
        return StrategyDecision(
            strategy=decision.strategy,
            action=Action.WAIT,
            reasons=reasons,
            confidence=decision.confidence,
            watched_level=decision.watched_level,
            visuals=dict(decision.visuals),
            details=details,
            setup_id=decision.setup_id,
        )

    @staticmethod
    def _annotate_liquidity_evidence(
        session: ActiveSymbolSession,
        decision: StrategyDecision,
    ) -> None:
        evidence = session.liquidity_evidence
        if evidence is None:
            return
        decision.details["liquidityEvidence"] = evidence.public()
        alignment = evidence.alignment_for(decision.action)
        if alignment is not None:
            decision.details["liquidityAlignment"] = alignment.public()

    @staticmethod
    def _annotate_flow_context(
        session: ActiveSymbolSession,
        decision: StrategyDecision,
    ) -> None:
        context = session.flow_context
        if context is None:
            return
        decision.details["multiHorizonFlow"] = context.public()
        alignment = context.alignment_for(decision.action)
        if alignment is not None:
            decision.details["flowAlignment"] = alignment.public()

    @staticmethod
    def _freshness_object_key(
        decision: StrategyDecision,
    ) -> tuple:
        details = decision.details or {}
        for key in (
            "zoneGeneration",
            "levelGeneration",
            "trendlineAnchor",
        ):
            value = details.get(key)
            if value is not None:
                if isinstance(value, (list, tuple)):
                    value = tuple(value)
                return (decision.strategy, key, str(value))

        zone = details.get("zone")
        if isinstance(zone, dict):
            low = zone.get("low")
            high = zone.get("high")
            if low is not None and high is not None:
                return (
                    decision.strategy,
                    "zone",
                    round(float(low), 8),
                    round(float(high), 8),
                )

        wall_price = details.get("wallPrice")
        if wall_price is not None:
            return (
                decision.strategy,
                "wall",
                str(details.get("wallSide") or ""),
                round(float(wall_price), 8),
            )

        return (
            decision.strategy,
            "watched",
            round(float(decision.watched_level or 0.0), 8),
        )

    @staticmethod
    def _freshness_fallback_trigger_price(
        decision: StrategyDecision,
    ) -> tuple[float | None, str]:
        details = decision.details or {}
        if decision.strategy == "trend_structure":
            value = details.get("reclaimPrice")
            if isinstance(value, (int, float)) and value > 0:
                return float(value), "reclaim_price"

        if decision.strategy == "level_breakout":
            value = details.get("acceptanceBoundary")
            if isinstance(value, (int, float)) and value > 0:
                return float(value), "breakout_boundary"

        if decision.strategy == "weak_level_rejection":
            zone = details.get("zone")
            if isinstance(zone, dict):
                if decision.action == Action.LONG:
                    value = zone.get("high")
                else:
                    value = zone.get("low")
                if isinstance(value, (int, float)) and value > 0:
                    return float(value), "rejection_boundary"

        if decision.strategy == "orderbook_density":
            value = details.get("wallPrice")
            if isinstance(value, (int, float)) and value > 0:
                return float(value), "density_wall"

        if decision.watched_level is not None and decision.watched_level > 0:
            return float(decision.watched_level), "watched_level"
        return None, "unknown"

    def _annotate_entry_freshness(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
        *,
        observed_at: float,
    ) -> None:
        state = str((decision.details or {}).get("state") or "")
        strategy = decision.strategy
        object_key = self._freshness_object_key(decision)
        anchor = session.entry_freshness_anchors.get(strategy)

        if state in ENTRY_FRESHNESS_RESET_STATES:
            session.entry_freshness_anchors.pop(strategy, None)
            session.entry_freshness_fingerprints.pop(strategy, None)
            return

        if state in ENTRY_FRESHNESS_TRIGGER_STATES.get(strategy, set()):
            if anchor is None or anchor.get("objectKey") != object_key:
                trigger_price = (
                    session.orderbook.mid
                    or session.last_price
                    or decision.watched_level
                )
                if trigger_price and trigger_price > 0:
                    anchor = {
                        "objectKey": object_key,
                        "triggerPrice": float(trigger_price),
                        "triggerTs": float(observed_at),
                        "source": f"{state}_state",
                    }
                    session.entry_freshness_anchors[strategy] = anchor

        if not decision.tradeable:
            return

        anchor = session.entry_freshness_anchors.get(strategy)
        if anchor is None or anchor.get("objectKey") != object_key:
            trigger_price, source = self._freshness_fallback_trigger_price(
                decision
            )
            anchor = {
                "objectKey": object_key,
                "triggerPrice": trigger_price,
                "triggerTs": None,
                "source": source,
            }
            session.entry_freshness_anchors[strategy] = anchor

        current_price = (
            decision.entry
            or session.orderbook.mid
            or session.last_price
            or 0.0
        )
        freshness = classify_entry_freshness(
            decision,
            trigger_price=anchor.get("triggerPrice"),
            trigger_ts=anchor.get("triggerTs"),
            current_price=float(current_price or 0.0),
            observed_ts=observed_at,
            source=str(anchor.get("source") or "unknown"),
        )
        decision.details["entryFreshness"] = freshness.public()

        fingerprint = (
            object_key,
            freshness.classification.value,
            (
                round(float(freshness.move_spent_ratio), 1)
                if freshness.move_spent_ratio is not None
                else None
            ),
        )
        if session.entry_freshness_fingerprints.get(strategy) == fingerprint:
            return
        session.entry_freshness_fingerprints[strategy] = fingerprint
        self._emit(
            "entry_freshness_changed",
            session.symbol,
            {
                "strategy": strategy,
                "setupId": (
                    decision.setup_id
                    or (
                        self._resolve_setup_id(session, decision)
                        if decision.tradeable
                        else None
                    )
                ),
                "state": state,
                "entryFreshness": freshness.public(),
                "decision": decision.public(),
            },
        )

    def _observe_wait_for_rearm(self, session: ActiveSymbolSession, strategy: str, now: float) -> None:
        if strategy not in session.consumed_setups:
            session.nontradeable_since.pop(strategy, None)
            return
        since = session.nontradeable_since.setdefault(strategy, now)
        if now - since < self.config.setup_reset_wait_seconds:
            return
        old_setup = session.consumed_setups.pop(strategy, None)
        session.nontradeable_since.pop(strategy, None)
        if old_setup:
            self._emit(
                "setup_rearmed",
                session.symbol,
                {"strategy": strategy, "previousSetupId": old_setup},
            )

    def _resolve_setup_id(self, session: ActiveSymbolSession, decision: StrategyDecision) -> str:
        if decision.setup_id:
            return decision.setup_id
        zone = decision.details.get("zone")
        if isinstance(zone, dict) and zone.get("low") is not None and zone.get("high") is not None:
            return (
                f"{decision.strategy}:{decision.action.value}:"
                f"{float(zone['low']):.10g}:{float(zone['high']):.10g}"
            )
        if decision.watched_level is not None:
            return (
                f"{decision.strategy}:{decision.action.value}:"
                f"{float(decision.watched_level):.10g}"
            )
        bucket = session.candles[-1].start_ms // 300_000 if session.candles else 0
        return f"{decision.strategy}:{decision.action.value}:window:{bucket}"

    async def _arbiter_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.config.arbiter_interval_seconds)
                if self.running:
                    self._arbitrate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("arbiter_error", None, {"error": str(exc)})

    def _arbitrate_once(self) -> None:
        opportunities: list[Opportunity] = []
        now = time()
        for event in self.broker.expire_pending(now):
            self._emit(
                "entry_cancelled",
                event.get("symbol"),
                event,
            )
        candidate_map = {item.symbol: item for item in self.candidates}

        for session in self.sessions.values():
            if (
                session.symbol in self.broker.positions
                or session.symbol in self.broker.pending_entries
            ):
                continue
            if (
                session.last_market_at <= 0
                or now - session.last_market_at
                > self.config.market_stale_seconds
            ):
                continue
            if not session.book_is_fresh(now):
                continue
            if not session.confirmed_candle_is_fresh(
                self.config.confirmed_candle_stale_seconds,
                now,
            ):
                continue

            for decision in session.decisions.values():
                if decision.strategy == "orderbook_density":
                    continue
                if not decision.tradeable or not self.strategy_enabled.get(decision.strategy, False):
                    continue

                setup_id = self._resolve_setup_id(session, decision)
                decision.setup_id = setup_id

                blocked_reason = self._setup_blocked_reason(session, decision.strategy, setup_id, now)
                if blocked_reason:
                    self._record_setup_blocked(session, decision, blocked_reason)
                    continue

                expectancy_snapshot = self.expectancy.snapshot(
                    decision.strategy,
                    min_samples=self.config.strategy_expectancy_min_samples,
                    minimum_expectancy_r=minimum_expectancy_r(
                        self.config,
                        decision.strategy,
                    ),
                )
                if (
                    self.config.enforce_strategy_expectancy_gate
                    and expectancy_snapshot["sampleReady"]
                    and expectancy_snapshot["status"] == "negative"
                ):
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        (
                            "strategy expectancy gate: "
                            f"{expectancy_snapshot['expectancyR']:.3f}R < "
                            f"{expectancy_snapshot['minimumExpectancyR']:.3f}R"
                        ),
                        diagnostics={
                            "expectancy": expectancy_snapshot,
                        },
                    )
                    continue

                allowed, portfolio_reason = self.broker.can_open(session.symbol)
                if not allowed:
                    self._risk_reject_if_changed(session, decision, portfolio_reason)
                    continue

                result = self.risk.build_plan(
                    session.symbol,
                    decision,
                    self.broker.balance,
                    session.orderbook,
                    self.broker.available_notional,
                    self.broker.available_risk_usd,
                    setup_id=setup_id,
                )
                if not result.allowed or result.plan is None:
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        result.reason,
                        diagnostics=result.diagnostics,
                    )
                    continue

                economics = (
                    result.plan.strategy_details.get("economics")
                    if isinstance(result.plan.strategy_details, dict)
                    else None
                )
                shadow_reasons = (
                    tuple(economics.get("shadowRejectReasons") or [])
                    if isinstance(economics, dict)
                    else ()
                )
                shadow_fingerprint = (
                    decision.strategy,
                    setup_id,
                    shadow_reasons,
                )
                if shadow_reasons:
                    if session.last_economic_shadow_fingerprint != shadow_fingerprint:
                        session.last_economic_shadow_fingerprint = shadow_fingerprint
                        self._emit(
                            "economic_shadow",
                            session.symbol,
                            {
                                "strategy": decision.strategy,
                                "setupId": setup_id,
                                "shadowRejectReasons": list(shadow_reasons),
                                "economics": economics,
                                "decision": decision.public(),
                            },
                            snapshot=True,
                        )
                else:
                    session.last_economic_shadow_fingerprint = None

                candidate = candidate_map.get(session.symbol)
                rank = candidate.activity_rank if candidate and candidate.activity_rank else 99
                score = self._opportunity_score(
                    decision,
                    rank,
                    candidate.activity_score if candidate else 0.0,
                )
                opportunities.append(
                    Opportunity(
                        score=score,
                        session=session,
                        decision=decision,
                        plan=result.plan,
                    )
                )

        if not opportunities:
            return

        best = max(opportunities, key=lambda item: item.score)
        allowed, reason = self.broker.can_open(best.session.symbol)
        if not allowed:
            self._risk_reject_if_changed(best.session, best.decision, reason)
            return

        best.session.last_risk_fingerprint = None
        best.session.last_blocked_fingerprint = None
        if best.plan.entry_mode == "maker_limit":
            pending = self.broker.place_pending(
                best.plan,
                min_trade_ts_ms=(
                    best.session.trades[-1].ts_ms
                    if best.session.trades
                    else None
                ),
            )
            self._emit(
                "entry_pending",
                best.session.symbol,
                {
                    "plan": best.plan.public(),
                    "pending": pending.public(),
                    "reasons": best.decision.reasons,
                    "visuals": best.decision.visuals,
                    "opportunityScore": best.score,
                    "opportunityQuality": float(
                        best.decision.details.get(
                            "setupQuality",
                            best.decision.confidence,
                        ) or 0.0
                    ),
                },
                snapshot=True,
            )
            return

        best.session.last_trade_at = now
        position = self.broker.open(best.plan, best.session.orderbook)
        self._record_opened_position(
            best.session,
            best.decision,
            best.plan.public(),
            position.public(),
            opportunity_score=best.score,
        )

    def _record_opened_position(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision | None,
        plan: dict,
        position: dict,
        *,
        opportunity_score: float | None = None,
    ) -> None:
        strategy_key = str(plan.get("strategy") or "")
        stats = self.strategy_stats.get(strategy_key)
        if stats is not None:
            stats["tradesOpened"] += 1
        strategy = self.strategies.get(strategy_key)
        if strategy is not None and decision is not None:
            strategy.mark_opened(session.symbol, decision)
        session.last_trade_at = time()
        self._emit(
            "trade_opened",
            session.symbol,
            {
                "plan": plan,
                "position": position,
                "reasons": decision.reasons if decision else [],
                "visuals": decision.visuals if decision else {},
                "opportunityScore": opportunity_score,
                "opportunityQuality": float(
                    decision.details.get(
                        "setupQuality",
                        decision.confidence,
                    ) or 0.0
                ) if decision else 0.0,
                "holdingRule": (
                    "resting maker partial near 1R; runner moves to net "
                    "breakeven; no-follow-through is strategy-specific"
                ),
            },
            snapshot=True,
        )

    @staticmethod
    def _opportunity_score(
        decision: StrategyDecision,
        activity_rank: int,
        activity_score: float = 0.0,
    ) -> float:
        # Geometry-derived net R/R was not predictive in the long paper run:
        # it describes payoff *if target is reached*, not the probability of
        # reaching it. Rank opportunities by strategy-specific setup quality
        # and use activity only as a small tie-breaker.
        quality = float(decision.details.get("setupQuality", decision.confidence) or 0.0)
        activity_bonus = max(0.0, 12 - min(activity_rank, 12)) * 0.5
        market_attention_bonus = max(0.0, min(activity_score, 100.0)) * 0.12
        return (
            quality * 100
            + decision.confidence * 15
            + activity_bonus
            + market_attention_bonus
        )

    def _setup_blocked_reason(
        self,
        session: ActiveSymbolSession,
        strategy: str,
        setup_id: str,
        now: float,
    ) -> str | None:
        cooldown = session.cooldown_until.get(strategy, 0.0)
        if now < cooldown:
            return f"strategy rearm cooldown {cooldown - now:.1f}s"
        if session.consumed_setups.get(strategy) == setup_id:
            return "same setup already consumed"
        return None

    def _record_setup_blocked(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
        reason: str,
    ) -> None:
        fingerprint = (decision.strategy, decision.setup_id, reason)
        if session.last_blocked_fingerprint == fingerprint:
            return
        session.last_blocked_fingerprint = fingerprint
        self._emit(
            "setup_blocked",
            session.symbol,
            {
                "strategy": decision.strategy,
                "setupId": decision.setup_id,
                "reason": reason,
                "decision": decision.public(),
            },
        )

    def _maybe_strategy_invalidation(
        self,
        session: ActiveSymbolSession,
    ) -> None:
        pos = self.broker.positions.get(session.symbol)
        if pos is None:
            return
        if time() - pos.opened_at < 5:
            return
        strategy = self.strategies.get(pos.strategy)
        if strategy is None:
            return
        decision = session.decisions.get(pos.strategy)
        reason = strategy.manage_position(
            side=pos.side,
            unrealized_pnl=pos.unrealized_pnl,
            opened_at=pos.opened_at,
            strategy_details=pos.strategy_details,
            decision=decision,
            trend=session.trend,
            last_price=session.last_price,
            book=session.orderbook,
            observed_at_ms=int(time() * 1000),
        )
        if not reason:
            return
        event = self.broker.close(
            session.symbol,
            session.orderbook,
            reason,
        )
        self._handle_broker_events(session, [event])

    def _mark_execution_from_market(
        self,
        session: ActiveSymbolSession,
        *,
        trade_ts_ms: int | None = None,
    ) -> None:
        pending_events = self.broker.mark_pending(
            session.symbol,
            session.last_price,
            trade_ts_ms=trade_ts_ms,
        )
        for event in pending_events:
            if event.get("event") == "entry_filled":
                strategy_key = str(event.get("strategy") or "")
                plan = dict(event.get("plan") or {})
                decision = session.decisions.get(strategy_key)
                plan_setup_id = str(plan.get("setup_id") or "")
                if (
                    decision is None
                    or str(decision.setup_id or "") != plan_setup_id
                ):
                    side = str(plan.get("side") or "")
                    decision = StrategyDecision(
                        strategy=strategy_key,
                        action=(
                            Action.LONG
                            if side == Side.LONG.value
                            else Action.SHORT
                        ),
                        reasons=["passive maker entry filled"],
                        entry=plan.get("setup_entry"),
                        stop=plan.get("stop"),
                        target=plan.get("target"),
                        setup_id=plan_setup_id or None,
                        details=dict(plan.get("strategy_details") or {}),
                    )
                self._record_opened_position(
                    session,
                    decision,
                    plan,
                    dict(event.get("position") or {}),
                )
            elif event.get("event") == "entry_cancelled":
                self._emit(
                    "entry_cancelled",
                    session.symbol,
                    event,
                    snapshot=True,
                )
        self._mark_position_from_book(session)

    def _mark_position_from_book(
        self,
        session: ActiveSymbolSession,
    ) -> None:
        if session.symbol not in self.broker.positions:
            return
        mark = (
            session.last_price
            or session.orderbook.mid
            or 0.0
        )
        if mark <= 0:
            return
        events = self.broker.mark(
            session.symbol,
            mark,
            session.orderbook,
        )
        self._handle_broker_events(session, events)

    def _handle_broker_events(self, session: ActiveSymbolSession, events: list[dict]) -> None:
        for event in events:
            event_type = event.get("event")
            if event_type == "partial_take":
                self._emit("partial_take", session.symbol, event, snapshot=True)
                continue
            if event_type == "trade_closed":
                strategy_key = str(event.get("strategy") or "")
                stats = self.strategy_stats.get(strategy_key)
                if stats is not None:
                    net = float(event.get("netPnl") or 0.0)
                    stats["tradesClosed"] += 1
                    stats["netPnl"] += net
                    if net > 0:
                        stats["wins"] += 1
                    elif net < 0:
                        stats["losses"] += 1
                self.expectancy.record(
                    strategy_key,
                    net_pnl_usd=float(
                        event.get("netPnl") or 0.0
                    ),
                    initial_risk_usd=float(
                        event.get("initialRiskUsd") or 0.0
                    ),
                )
                self._consume_setup(
                    session,
                    str(event.get("strategy") or ""),
                    str(event.get("setupId") or ""),
                )
                self._emit("trade_closed", session.symbol, event, snapshot=True)

    def _consume_setup(self, session: ActiveSymbolSession, strategy: str, setup_id: str) -> None:
        if not strategy or not setup_id:
            return
        session.consumed_setups[strategy] = setup_id
        session.cooldown_until[strategy] = time() + self.config.setup_rearm_seconds
        session.nontradeable_since.pop(strategy, None)
        self._emit(
            "setup_consumed",
            session.symbol,
            {
                "strategy": strategy,
                "setupId": setup_id,
                "cooldownSeconds": self.config.setup_rearm_seconds,
            },
        )

    def _cancel_all_pending(self, reason: str) -> None:
        for event in self.broker.cancel_all_pending(reason):
            self._emit(
                "entry_cancelled",
                event.get("symbol"),
                event,
            )

    def _close_all_positions(self, reason: str) -> None:
        for symbol in list(self.broker.positions):
            session = self.sessions.get(symbol)
            book = session.orderbook if session else OrderBook()
            event = self.broker.close(symbol, book, reason)
            if session:
                self._handle_broker_events(session, [event])
            else:
                self._emit("trade_closed", symbol, event)

    def _risk_reject_if_changed(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision | None,
        reason: str,
        *,
        diagnostics: dict | None = None,
    ) -> None:
        fingerprint = (
            decision.strategy if decision else "portfolio",
            decision.setup_id if decision else None,
            reason,
        )
        if session.last_risk_fingerprint == fingerprint:
            return
        session.last_risk_fingerprint = fingerprint
        if decision is not None:
            stats = self.strategy_stats.get(decision.strategy)
            if stats is not None:
                stats["riskRejects"] += 1
                resolved_setup_id = (
                    decision.setup_id
                    or self._resolve_setup_id(session, decision)
                )
                seen = self._seen_risk_rejected_setups.setdefault(
                    decision.strategy,
                    set(),
                )
                setup_key = (
                    session.symbol,
                    str(resolved_setup_id),
                )
                if setup_key not in seen:
                    seen.add(setup_key)
                    stats["uniqueRiskRejectedSetups"] += 1
        self._emit(
            "risk_reject",
            session.symbol,
            {
                "strategy": decision.strategy if decision else None,
                "reason": reason,
                "decision": decision.public() if decision else None,
                "diagnostics": {
                    "balance": self.broker.balance,
                    "availableNotionalUsd": self.broker.available_notional,
                    "availableRiskUsd": self.broker.available_risk_usd,
                    "portfolioExposureUsd": self.broker.total_exposure,
                    "portfolioOpenRiskUsd": self.broker.open_risk_usd,
                    "bestBid": session.orderbook.best_bid,
                    "bestAsk": session.orderbook.best_ask,
                    "spreadPct": session.orderbook.spread_pct,
                    "setupEntry": decision.entry if decision else None,
                    "stop": decision.stop if decision else None,
                    "target": decision.target if decision else None,
                    **(diagnostics or {}),
                },
            },
            snapshot=True,
        )

    def _record_decision_if_changed(self, session: ActiveSymbolSession, decision: StrategyDecision) -> None:
        fingerprint = (
            decision.action.value,
            decision.setup_id,
            round(decision.watched_level or 0, 8),
            round(decision.entry or 0, 8),
            decision.details.get("state"),
            tuple(decision.reasons),
        )
        if session.decision_fingerprints.get(decision.strategy) == fingerprint:
            return
        session.decision_fingerprints[decision.strategy] = fingerprint
        stats = self.strategy_stats.get(decision.strategy)
        if stats is not None:
            stats["decisions"] = int(stats["decisions"]) + 1
            stats["decisionUpdates"] = int(stats["decisionUpdates"]) + 1
            state_counts = stats.get("stateCounts")
            if isinstance(state_counts, dict):
                state_key = str(decision.details.get("state") or "unknown")
                state_counts[state_key] = int(state_counts.get(state_key, 0)) + 1
            if decision.tradeable:
                stats["tradeableSignals"] += 1
                resolved_setup_id = (
                    decision.setup_id
                    or self._resolve_setup_id(session, decision)
                )
                seen = self._seen_tradeable_setups.setdefault(
                    decision.strategy,
                    set(),
                )
                setup_key = (
                    session.symbol,
                    str(resolved_setup_id),
                )
                if setup_key not in seen:
                    seen.add(setup_key)
                    stats["uniqueTradeableSetups"] += 1
            else:
                stats["waitDecisions"] += 1
        observed_at_ms = int(time() * 1000)
        payload = decision.public()
        payload["marketContext"] = session.market_context_public()
        payload["trace"] = build_decision_trace(
            decision,
            session.trend,
            observed_at_ms,
            market_context=session.market_context_public(),
        )
        self._emit("decision", session.symbol, payload)

    def _emit(self, event: str, symbol: str | None, payload: dict, snapshot: bool = False) -> None:
        row = {"ts": time(), "event": event, "symbol": symbol, "payload": payload}
        self.events.appendleft(row)
        stored = dict(payload)
        if snapshot and symbol in self.sessions:
            stored["market"] = self.sessions[symbol].market_snapshot()
        self.recorder.record(event, symbol, stored)

    def public_state(self, selected_symbol: str | None = None) -> dict:
        working = list(self.sessions)
        if selected_symbol not in self.sessions:
            selected_symbol = working[0] if working else None
        market = self.sessions[selected_symbol].market_snapshot() if selected_symbol else None
        candidate_map = {x.symbol: x for x in self.candidates}
        if market is not None and selected_symbol is not None:
            selected_candidate = candidate_map.get(selected_symbol)
            market["activityProfile"] = (
                selected_candidate.public() if selected_candidate else None
            )
        now = time()
        working_rows = []

        for symbol in working:
            candidate = candidate_map.get(symbol)
            session = self.sessions[symbol]
            position = self.broker.positions.get(symbol)
            working_rows.append(
                {
                    "symbol": symbol,
                    "turnover24h": candidate.turnover_24h if candidate else None,
                    "change24h": candidate.change_24h if candidate else None,
                    "activityChange": candidate.activity_change if candidate else None,
                    "activityRank": candidate.activity_rank if candidate else None,
                    "activityScore": candidate.activity_score if candidate else None,
                    "correlation1hBtc": candidate.correlation_1h_btc if candidate else None,
                    "volume24h": candidate.volume_24h if candidate else None,
                    "tradeCount24h": candidate.trade_count_24h if candidate else None,
                    "lastPrice": session.last_price,
                    "trend": session.trend.value,
                    "htfBias": (
                        session.htf_bias.bias.value
                        if session.htf_bias is not None
                        else None
                    ),
                    "localRegime": (
                        session.local_regime.regime.value
                        if session.local_regime is not None
                        else None
                    ),
                    "position": position.public() if position else None,
                    "activeAgeSeconds": now - session.activated_at,
                    "marketAgeSeconds": now - session.last_market_at if session.last_market_at > 0 else None,
                    "engaged": self._session_engaged(session),
                }
            )

        run_started = self._run_started_at
        run_deadline = self._run_deadline_at
        remaining = max(0.0, run_deadline - now) if self.running and run_deadline else 0.0
        elapsed = max(0.0, now - run_started) if run_started else 0.0

        return {
            "botRunning": self.running,
            "mode": "paper",
            "marketHealth": self.market_health(),
            "run": {
                "label": self.config.run_label,
                "configuredDurationSeconds": self.config.paper_run_duration_seconds,
                "startedAt": run_started,
                "deadlineAt": run_deadline,
                "elapsedSeconds": elapsed,
                "remainingSeconds": remaining,
                "lastSummary": self._last_run_summary,
            },
            "balance": self.broker.balance,
            "totalPnl": self.broker.total_pnl,
            "positions": [x.public() for x in self.broker.positions.values()],
            "pendingEntries": [
                x.public()
                for x in self.broker.pending_entries.values()
            ],
            "closedTrades": self.broker.closed_trades[-30:],
            "portfolio": {
                "totalExposure": self.broker.total_exposure,
                "grossLeverage": (
                    self.broker.total_exposure / self.broker.balance
                    if self.broker.balance > 0
                    else 0.0
                ),
                "availableNotional": self.broker.available_notional,
                "pendingExposureUsd": self.broker.pending_exposure_usd,
                "pendingRiskUsd": self.broker.pending_risk_usd,
                "openStructuralRiskUsd": (
                    self.broker.open_structural_risk_usd
                ),
                "openCostReserveUsd": self.broker.open_cost_reserve_usd,
                "openRiskUsd": self.broker.open_risk_usd,
                "availableRiskUsd": self.broker.available_risk_usd,
            },
            "working": working_rows,
            "candidates": [x.public() for x in self.candidates],
            "market": market,
            "events": list(self.events)[:100],
            "strategies": [
                {
                    "key": key,
                    "label": strategy.label,
                    "enabled": self.strategy_enabled[key],
                    "stats": dict(self.strategy_stats.get(key) or {}),
                    "expectancy": self.expectancy.snapshot(
                        key,
                        min_samples=self.config.strategy_expectancy_min_samples,
                        minimum_expectancy_r=minimum_expectancy_r(
                            self.config,
                            key,
                        ),
                    ),
                }
                for key, strategy in self.strategies.items()
            ],
            "strategyAnalytics": {
                key: dict(value)
                for key, value in self.strategy_stats.items()
            },
            "risk": {
                "minNetProfitUsd": self.config.min_net_profit_usd,
                "minNetProfitEquityFraction": self.config.min_net_profit_equity_fraction,
                "enforceMinNetProfitGate": self.config.enforce_min_net_profit_gate,
                "minNetRewardRisk": self.config.min_net_reward_risk,
                "enforceNetRewardRiskGate": self.config.enforce_net_reward_risk_gate,
                "riskFraction": self.config.risk_fraction,
                "maxTradeAllInLossFraction": self.config.max_trade_all_in_loss_fraction,
                "maxTotalRiskFraction": self.config.max_total_risk_fraction,
                "maxPortfolioLeverage": self.config.max_leverage,
                "maxPositionLeverage": self.config.max_position_leverage,
                "maxPositionExposureFraction": self.config.max_position_exposure_fraction,
                "maxEntryDriftBps": self.config.max_entry_drift_bps,
                "confirmedCandleStaleSeconds": self.config.confirmed_candle_stale_seconds,
                "takerFeeRate": self.config.taker_fee_rate,
                "slippageBps": self.config.slippage_bps,
                "makerFillConfirmationBps": self.config.maker_fill_confirmation_bps,
                "passiveEntryEnabled": self.config.passive_entry_enabled,
                "passiveEntryTimeoutSeconds": self.config.passive_entry_timeout_seconds,
                "maxWinnerCostShare": self.config.max_winner_cost_share,
                "winnerCostShareGateEnabled": self.config.enforce_winner_cost_share_gate,
                "minFirstTakeMovePct": self.config.min_first_take_move_pct,
                "firstTakeMoveGateEnabled": self.config.enforce_min_first_take_move_gate,
                "partialTakeAtR": self.config.partial_take_at_r,
                "partialTakeFraction": self.config.partial_take_fraction,
                "runnerTargetR": self.config.runner_target_r,
                "sessionLossLimitEnabled": self.config.enforce_session_loss_limit,
                "sessionLossLimitFraction": self.config.max_daily_loss_fraction,
                "strategyExpectancyGateEnabled": self.config.enforce_strategy_expectancy_gate,
                "strategyExpectancyMinSamples": self.config.strategy_expectancy_min_samples,
            },
            "sessionFile": str(self.recorder.path),
        }
