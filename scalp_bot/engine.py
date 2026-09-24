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
from .research_policy import (
    PolicyAssessment,
    PolicyMode,
    ResearchPolicyRuntime,
)
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
    FormingCandleContext,
    MarketContext,
    MarketStructure,
    SelectionPriority,
    SemanticCandidateAssessment,
    Strategy,
    build_execution_context,
    build_forming_candle_context,
    build_liquidity_evidence,
    build_market_structure,
    build_structure_context,
    build_multi_horizon_flow_context,
    classify_context_trend,
    classify_entry_freshness,
    classify_htf_bias,
    classify_local_regime,
    compute_trade_flow,
    assess_candidate,
    assess_session_candidates,
    build_selection_priority,
)


ACTIVE_SETUP_STATES = {
    "found",
    "persisting",
    "approach",
    "pullback",
    "pressure",
    "armed",
    "test",
    "reclaim",
    "defended",
    "reject",
    "reaction",
    "break",
    "impulse",
}

ENTRY_FRESHNESS_TRIGGER_STATES = {
    "trend_structure": {"pullback", "armed", "test", "reclaim"},
    "weak_level_rejection": {"test", "reject"},
    "level_breakout": {"pressure", "armed", "break"},
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
    forming_candle_context: FormingCandleContext | None = None
    forming_kline_start_ms: int = 0
    forming_kline_observed_at_ms: int = 0
    forming_trade_overlay_updates: int = 0
    forming_trade_last_ts_ms: int = 0
    forming_tape_only_start_ms: int = 0
    forming_price_source: str = "kline"
    forming_volume_source: str = "kline_snapshot"
    market_context: MarketContext | None = None
    market_context_fingerprint: tuple | None = None
    market_context_semantic_fingerprint: tuple | None = None
    last_market_context_event_at: float = 0.0
    market_context_changes_suppressed: int = 0
    market_context_changes_pending: int = 0
    market_context_events_emitted: int = 0
    entry_freshness_anchors: dict[str, dict] = field(default_factory=dict)
    entry_freshness_fingerprints: dict[str, tuple] = field(default_factory=dict)
    structure: MarketStructure | None = None
    static_analysis_key: tuple | None = None
    static_analysis_rebuilds: int = 0
    live_fast_path_reuses: int = 0
    static_analysis_rebuilt_at_ms: int = 0
    last_analysis_mode: str = "uninitialized"
    decisions: dict[str, StrategyDecision] = field(default_factory=dict)
    decision_fingerprints: dict[str, tuple] = field(default_factory=dict)
    strategy_states: dict[str, str] = field(default_factory=dict)
    strategy_state_started_at: dict[str, float] = field(default_factory=dict)
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
    arbiter_block_fingerprints: dict[str, tuple] = field(
        default_factory=dict
    )
    research_policy_fingerprints: dict[str, tuple] = field(
        default_factory=dict
    )

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

    def analysis_runtime_public(self) -> dict:
        return {
            "mode": self.last_analysis_mode,
            "staticAnalysisRebuilds": self.static_analysis_rebuilds,
            "liveFastPathReuses": self.live_fast_path_reuses,
            "marketContextEventsEmitted": (
                self.market_context_events_emitted
            ),
            "marketContextChangesSuppressed": (
                self.market_context_changes_suppressed
            ),
            "staticAnalysisRebuiltAtMs": (
                self.static_analysis_rebuilt_at_ms or None
            ),
            "staticAnalysisKey": (
                list(self.static_analysis_key)
                if self.static_analysis_key is not None
                else None
            ),
        }

    def market_context_public(self) -> dict:
        if self.market_context is not None:
            return self.market_context.public()
        return {
            "schemaVersion": 1,
            "symbol": self.symbol,
            "lastPrice": self.last_price,
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
            "structureContext": None,
            "executionContext": None,
        }

    def market_snapshot(self) -> dict:
        now_ms = int(time() * 1000)
        return {
            "symbol": self.symbol,
            "lastPrice": self.last_price,
            "trend": self.trend.value,
            "marketContext": self.market_context_public(),
            "analysisRuntime": self.analysis_runtime_public(),
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
            "analysisRuntime": self.analysis_runtime_public(),
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
    priority: SelectionPriority
    arbitration: SemanticCandidateAssessment
    session: ActiveSymbolSession
    decision: StrategyDecision
    plan: object
    position_action: str = "open"


class TradingEngine:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.rest = BybitRestClient(config)
        self.risk = RiskEngine(config)
        self.broker = PaperBroker(config)
        self.recorder = SessionRecorder(config.session_dir)
        self.research_policy = ResearchPolicyRuntime.from_settings(
            path=config.research_policy_file,
            mode=config.research_policy_mode,
        )
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
                "positionAdds": 0,
                "tradesClosed": 0,
                "wins": 0,
                "losses": 0,
                "netPnl": 0.0,
                "researchPolicyShadowMatches": 0,
                "researchPolicyBlockedMatches": 0,
                "stateCounts": {},
                "sideRegime": {
                    "long": {},
                    "short": {},
                },
            }
            for x in DEFAULT_STRATEGIES
        }
        for key, probe_fraction in (
            (
                "level_breakout",
                config.breakout_probe_risk_fraction,
            ),
            (
                "weak_level_rejection",
                config.weak_level_rejection_probe_risk_fraction,
            ),
        ):
            staged_strategy = self.strategies.get(key)
            if staged_strategy is not None:
                staged_enabled = config.staged_entries_enabled
                if key == "level_breakout":
                    staged_enabled = (
                        staged_enabled
                        and config.breakout_staged_entries_enabled
                    )
                elif key == "weak_level_rejection":
                    staged_enabled = (
                        staged_enabled
                        and config.weak_level_rejection_staged_entries_enabled
                    )
                setattr(
                    staged_strategy,
                    "staged_entries_enabled",
                    staged_enabled,
                )
                setattr(
                    staged_strategy,
                    "probe_risk_fraction",
                    probe_fraction,
                )

        breakout_strategy = self.strategies.get("level_breakout")
        if breakout_strategy is not None:
            setattr(
                breakout_strategy,
                "retest_tolerance_bps",
                config.breakout_retest_tolerance_bps,
            )
            setattr(
                breakout_strategy,
                "retest_response_min_bps",
                config.breakout_retest_response_min_bps,
            )
            setattr(
                breakout_strategy,
                "hold_without_retest_seconds",
                config.breakout_hold_without_retest_seconds,
            )
            setattr(
                breakout_strategy,
                "absorption_efficiency_threshold",
                config.breakout_absorption_efficiency_threshold,
            )
            setattr(
                breakout_strategy,
                "min_directional_response_bps",
                config.breakout_min_directional_response_bps,
            )

        rejection_strategy = self.strategies.get(
            "weak_level_rejection"
        )
        if rejection_strategy is not None:
            setattr(
                rejection_strategy,
                "micro_response_min_bps",
                config.weak_level_rejection_micro_response_min_bps,
            )
            setattr(
                rejection_strategy,
                "micro_response_min_seconds",
                config.weak_level_rejection_micro_response_min_seconds,
            )
            setattr(
                rejection_strategy,
                "micro_response_max_seconds",
                config.weak_level_rejection_micro_response_max_seconds,
            )

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
            if self.research_policy.active:
                self._emit(
                    "research_policy_activated",
                    None,
                    self.research_policy.public(),
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
            "economicCalibrationMinGroupSamples": (
                self.config.economic_calibration_min_group_samples
            ),
            "economicCalibrationMinSegmentSamples": (
                self.config.economic_calibration_min_segment_samples
            ),
            "researchPolicy": self.research_policy.public(),
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
            "tradeableStrategies": [
                key
                for key, enabled in self.strategy_enabled.items()
                if enabled and key != "orderbook_density"
            ],
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
        if key == "orderbook_density" and not enabled:
            for session in self.sessions.values():
                session.liquidity_evidence = None
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

    def _evaluation_interval_seconds(
        self,
        session: ActiveSymbolSession,
    ) -> float:
        idle = max(
            0.05,
            float(self.config.evaluation_idle_interval_seconds),
        )
        engaged = max(
            0.05,
            float(self.config.evaluation_engaged_interval_seconds),
        )
        if self._session_engaged(session):
            return min(idle, engaged)
        return idle

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
        self._refresh_market_context(
            session,
            closed_1m=closed_1m,
            closed_5m=session.context_5m,
            closed_15m=session.context_15m,
            closed_1h=session.context_1h,
            commit=False,
        )
        self._commit_market_context(
            session,
            observed_at_ms=int(now * 1000),
            emit=False,
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
                    # REST context may correct the latest confirmed HTF bar.
                    # Force the next live evaluation to rebuild structural
                    # geometry from that refreshed confirmed-candle snapshot.
                    session.static_analysis_key = None
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
                    ticks: list[TradeTick] = []
                    for row in rows:
                        tick = TradeTick(
                            ts_ms=int(
                                row.get("T") or time() * 1000
                            ),
                            price=float(row["p"]),
                            size=float(row["v"]),
                            side=str(row.get("S") or ""),
                        )
                        session.trades.append(tick)
                        self._overlay_trade_on_forming_candle(
                            session,
                            tick,
                        )
                        ticks.append(tick)

                    session.last_price = ticks[-1].price
                    session.last_trade_stream_at = wall_now
                    prune_trades(
                        session.trades,
                        ticks[-1].ts_ms,
                        self.config.trade_buffer_seconds,
                    )

                    # Pending maker orders must consume the newest tape before
                    # they are allowed to fill. Otherwise the same trade batch
                    # can invalidate a setup and fill its stale limit before
                    # the next 0.20s strategy evaluation.
                    if symbol in self.broker.pending_entries:
                        session.last_eval = monotonic()
                        await self._evaluate(session)

                    # A publicTrade websocket payload may contain several
                    # executions. Replay them in order so a maker entry/target
                    # crossing in an earlier row is not lost just because the
                    # final trade retraced.
                    for tick in ticks:
                        session.last_price = tick.price
                        self._mark_execution_from_market(
                            session,
                            trade_ts_ms=tick.ts_ms,
                            trade_price=tick.price,
                        )
                    session.last_price = ticks[-1].price

            now = monotonic()
            evaluation_interval = self._evaluation_interval_seconds(
                session
            )
            if now - session.last_eval >= evaluation_interval:
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

    @staticmethod
    def _overlay_trade_on_forming_candle(
        session: ActiveSymbolSession,
        trade: TradeTick,
    ) -> bool:
        minute_start = (
            trade.ts_ms // 60_000 * 60_000
        )
        forming = next(
            (
                candle
                for candle in reversed(session.candles)
                if (
                    not candle.confirmed
                    and candle.start_ms == minute_start
                )
            ),
            None,
        )
        if forming is None:
            # At a minute boundary the trade stream may lead the kline topic.
            # If this symbol was already active before the new minute began,
            # the first observed execution is a valid provisional minute open.
            # Mid-minute activations deliberately do not synthesize a candle
            # because their first observed trade is not necessarily the true
            # minute open.
            active_before_minute = (
                int(session.activated_at * 1000)
                <= minute_start
            )
            if not session.candles or not active_before_minute:
                return False
            forming = Candle(
                start_ms=minute_start,
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                volume=trade.size,
                turnover=trade.notional,
                confirmed=False,
            )
            session.candles.append(forming)
            session.candles.sort(
                key=lambda candle: candle.start_ms
            )
            session.forming_tape_only_start_ms = minute_start
            session.forming_kline_start_ms = 0
            session.forming_kline_observed_at_ms = 0
            session.forming_price_source = "tape_provisional"
            session.forming_volume_source = "tape_provisional"
            session.forming_trade_overlay_updates = 1
            session.forming_trade_last_ts_ms = trade.ts_ms
            return True

        forming.high = max(forming.high, trade.price)
        forming.low = min(forming.low, trade.price)
        forming.close = trade.price
        if (
            session.forming_tape_only_start_ms
            == minute_start
        ):
            # Before the first kline snapshot, tape is the only cumulative
            # source for this minute and may safely build provisional volume.
            forming.volume += trade.size
            forming.turnover += trade.notional
            session.forming_price_source = "tape_provisional"
            session.forming_volume_source = "tape_provisional"
        else:
            # Once a kline snapshot exists, only overlay price geometry. Its
            # cumulative volume/turnover already includes prior executions.
            session.forming_price_source = "hybrid_tape"
            session.forming_volume_source = "kline_snapshot"

        session.forming_trade_overlay_updates += 1
        session.forming_trade_last_ts_ms = max(
            session.forming_trade_last_ts_ms,
            trade.ts_ms,
        )
        return True

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

        forming = max(
            (
                candle
                for candle in session.candles
                if not candle.confirmed
            ),
            key=lambda candle: candle.start_ms,
            default=None,
        )
        if forming is not None:
            snapshot_observed_at_ms = int(
                message.get("ts")
                or time() * 1000
            )
            session.forming_kline_start_ms = (
                forming.start_ms
            )
            session.forming_kline_observed_at_ms = (
                snapshot_observed_at_ms
            )
            session.forming_tape_only_start_ms = 0
            session.forming_price_source = "kline"
            session.forming_volume_source = "kline_snapshot"
            session.forming_trade_overlay_updates = 0
            session.forming_trade_last_ts_ms = 0

            # If a kline update is delivered after newer public trades, do not
            # let the slower topic move live OHLC backwards. Re-apply only the
            # tape executions newer than the kline snapshot timestamp.
            for trade in session.trades:
                if (
                    trade.ts_ms > snapshot_observed_at_ms
                    and forming.start_ms
                    <= trade.ts_ms
                    < forming.start_ms + 60_000
                ):
                    self._overlay_trade_on_forming_candle(
                        session,
                        trade,
                    )
        else:
            session.forming_kline_start_ms = 0
            session.forming_kline_observed_at_ms = 0
            session.forming_trade_overlay_updates = 0
            session.forming_trade_last_ts_ms = 0
            session.forming_tape_only_start_ms = 0
            session.forming_price_source = "kline"
            session.forming_volume_source = "kline_snapshot"

    @staticmethod
    def _closed_candle_source_key(
        candles: list[Candle],
    ) -> tuple:
        if not candles:
            return (0, None)
        latest = candles[-1]
        return (
            len(candles),
            latest.start_ms,
            round(latest.open, 10),
            round(latest.high, 10),
            round(latest.low, 10),
            round(latest.close, 10),
            round(latest.volume, 6),
        )

    @classmethod
    def _static_analysis_source_key(
        cls,
        closed_1m: list[Candle],
        closed_5m: list[Candle],
        closed_15m: list[Candle],
        closed_1h: list[Candle],
    ) -> tuple:
        return (
            cls._closed_candle_source_key(closed_1m),
            cls._closed_candle_source_key(closed_5m),
            cls._closed_candle_source_key(closed_15m),
            cls._closed_candle_source_key(closed_1h),
        )

    async def _evaluate(self, session: ActiveSymbolSession) -> None:
        if not session.candles:
            return

        closed_1m = [x for x in session.candles if x.confirmed]
        closed_5m = [x for x in session.context_5m if x.confirmed]
        closed_15m = [x for x in session.context_15m if x.confirmed]
        closed_1h = [x for x in session.context_1h if x.confirmed]
        if not closed_1m:
            return

        now = time()
        now_ms = int(now * 1000)
        forming_1m = max(
            (candle for candle in session.candles if not candle.confirmed),
            key=lambda candle: candle.start_ms,
            default=None,
        )
        session.forming_candle_context = build_forming_candle_context(
            forming_1m,
            closed_1m,
            observed_at_ms=now_ms,
            recent_trades=list(session.trades),
            tape_updates=(
                session.forming_trade_overlay_updates
                if (
                    forming_1m is not None
                    and (
                        session.forming_kline_start_ms
                        in {0, forming_1m.start_ms}
                    )
                )
                else 0
            ),
            last_trade_ts_ms=(
                session.forming_trade_last_ts_ms
                or None
            ),
            kline_snapshot_observed_at_ms=(
                session.forming_kline_observed_at_ms
                if (
                    forming_1m is not None
                    and session.forming_kline_start_ms
                    == forming_1m.start_ms
                    and session.forming_kline_observed_at_ms > 0
                )
                else None
            ),
            price_source=session.forming_price_source,
            volume_source=session.forming_volume_source,
        )

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

        if not session.confirmed_candle_is_fresh(
            self.config.confirmed_candle_stale_seconds,
            now,
        ):
            session.last_analysis_mode = "stale_candle"
            self._commit_market_context(
                session,
                observed_at_ms=now_ms,
            )
            age = session.confirmed_candle_age_seconds(now)
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
                        "evidenceOnly": (
                            key == "orderbook_density"
                        ),
                    },
                )
                self._annotate_decision_context(session, decision)
                session.decisions[key] = decision
                self._record_decision_if_changed(session, decision)
            return

        static_key = self._static_analysis_source_key(
            closed_1m,
            closed_5m,
            closed_15m,
            closed_1h,
        )
        static_rebuild = (
            session.structure is None
            or session.static_analysis_key != static_key
            or session.local_regime is None
            or session.htf_bias is None
        )
        if static_rebuild:
            self._refresh_market_context(
                session,
                closed_1m=closed_1m,
                closed_5m=closed_5m,
                closed_15m=closed_15m,
                closed_1h=closed_1h,
                commit=False,
                observed_at_ms=now_ms,
            )
            # Structural geometry is derived only from confirmed candles.
            # Use the last confirmed close as the stable construction
            # reference; the live price is applied below by lifecycle/context.
            structure_reference = closed_1m[-1].close
            session.structure = build_market_structure(
                closed_1m,
                closed_15m,
                structure_reference,
                context_5m=closed_5m,
                context_1h=closed_1h,
            )
            session.static_analysis_key = static_key
            session.static_analysis_rebuilds += 1
            session.static_analysis_rebuilt_at_ms = now_ms
            session.last_analysis_mode = "static_rebuild"
        else:
            session.live_fast_path_reuses += 1
            session.last_analysis_mode = "live_fast_path"

        reference_price = session.orderbook.mid or session.last_price
        session.structure = session.level_tracker.update(
            session.structure,
            closed_1m,
            reference_price,
            now_ms,
        )

        # Density is the liquidity provider, so the preliminary context passed
        # into it deliberately excludes the previous cycle's liquidity state.
        session.liquidity_evidence = None
        self._commit_market_context(
            session,
            observed_at_ms=now_ms,
            emit=False,
        )

        density_decision: StrategyDecision | None = None
        density = self.strategies.get("orderbook_density")
        if (
            density is not None
            and self.strategy_enabled.get("orderbook_density", False)
        ):
            if not session.book_is_fresh(now):
                raw_density = StrategyDecision(
                    strategy="orderbook_density",
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
            else:
                try:
                    raw_density = density.evaluate(
                        closed_1m,
                        session.orderbook,
                        session.trend,
                        symbol=session.symbol,
                        trades=list(session.trades),
                        structure=session.structure,
                        market_context=session.market_context,
                        observed_at_ms=now_ms,
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    previous = session.decisions.get(
                        "orderbook_density"
                    )
                    if (
                        previous is None
                        or previous.details.get("error") != error
                    ):
                        self._emit(
                            "strategy_error",
                            session.symbol,
                            {
                                "strategy": "orderbook_density",
                                "error": error,
                            },
                            snapshot=True,
                        )
                    raw_density = StrategyDecision(
                        strategy="orderbook_density",
                        action=Action.WAIT,
                        reasons=[f"Ошибка стратегии: {error}"],
                        details={
                            "state": "error",
                            "error": error,
                            "evidenceOnly": True,
                        },
                    )

            self._annotate_flow_context(
                session,
                raw_density,
            )
            session.liquidity_evidence = build_liquidity_evidence(
                raw_density
            )
            raw_density.details["liquidityEvidence"] = (
                session.liquidity_evidence.public()
            )
            density_decision = self._density_as_evidence_only(
                raw_density
            )

        # This is the canonical context shared by all tradeable playbooks.
        self._commit_market_context(
            session,
            observed_at_ms=now_ms,
        )

        if density_decision is not None:
            self._annotate_decision_context(
                session,
                density_decision,
            )
            session.decisions["orderbook_density"] = density_decision
            self._record_decision_if_changed(
                session,
                density_decision,
            )

        for key, strategy in self.strategies.items():
            if key == "orderbook_density":
                continue
            if not self.strategy_enabled.get(key, False):
                continue
            try:
                decision = strategy.evaluate(
                    closed_1m,
                    session.orderbook,
                    session.trend,
                    symbol=session.symbol,
                    trades=list(session.trades),
                    structure=session.structure,
                    market_context=session.market_context,
                    observed_at_ms=now_ms,
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
            self._annotate_liquidity_evidence(
                session,
                decision,
            )
            self._annotate_entry_freshness(
                session,
                decision,
                observed_at=now,
            )
            self._annotate_decision_context(
                session,
                decision,
            )

            if decision.tradeable:
                decision.setup_id = self._resolve_setup_id(
                    session,
                    decision,
                )
                session.last_signal_at = now
                session.nontradeable_since.pop(key, None)
            else:
                self._observe_wait_for_rearm(
                    session,
                    key,
                    now,
                )

            session.decisions[key] = decision
            self._record_decision_if_changed(session, decision)

        self._validate_pending_entry(session)
        self._maybe_strategy_invalidation(session)

    @staticmethod
    def _trade_buffer_seconds(
        session: ActiveSymbolSession,
    ) -> float:
        if len(session.trades) < 2:
            return 0.0
        return max(
            0.0,
            (session.trades[-1].ts_ms - session.trades[0].ts_ms)
            / 1000,
        )

    def _build_market_context(
        self,
        session: ActiveSymbolSession,
        *,
        observed_at_ms: int,
    ) -> MarketContext:
        observed_at = observed_at_ms / 1000
        reference_price = (
            session.orderbook.mid
            or session.last_price
            or 0.0
        )
        execution = build_execution_context(
            book=session.orderbook,
            book_fresh=session.book_is_fresh(observed_at),
            book_synced=session.book_synced,
            book_age_seconds=session.book_age_seconds(observed_at),
            candle_fresh=session.confirmed_candle_is_fresh(
                session.confirmed_candle_stale_after_seconds,
                observed_at,
            ),
            candle_age_seconds=session.confirmed_candle_age_seconds(
                observed_at
            ),
            trade_buffer_seconds=self._trade_buffer_seconds(session),
        )
        return MarketContext(
            symbol=session.symbol,
            observed_at_ms=observed_at_ms,
            last_price=session.last_price,
            legacy_trend=session.trend,
            htf_bias=session.htf_bias,
            local_regime=session.local_regime,
            flow=session.flow_context,
            liquidity=session.liquidity_evidence,
            structure=build_structure_context(
                session.structure,
                float(reference_price),
            ),
            execution=execution,
            forming_candle=session.forming_candle_context,
        )

    @staticmethod
    def _context_level_digest(level) -> tuple | None:
        if level is None:
            return None
        return (
            str(level.kind),
            str(level.generation_id or ""),
            round(float(level.center), 8),
            str(level.lifecycle),
        )

    @classmethod
    def _market_context_semantic_fingerprint(
        cls,
        context: MarketContext,
    ) -> tuple:
        structure = context.structure
        liquidity = context.liquidity
        return (
            context.legacy_trend.value,
            (
                context.htf_bias.bias.value
                if context.htf_bias is not None
                else None
            ),
            (
                context.htf_bias.alignment
                if context.htf_bias is not None
                else None
            ),
            (
                context.local_regime.regime.value
                if context.local_regime is not None
                else None
            ),
            (
                context.local_regime.direction.value
                if context.local_regime is not None
                else None
            ),
            (
                liquidity.state.value
                if liquidity is not None
                else None
            ),
            (
                liquidity.directional_bias.value
                if liquidity is not None
                else None
            ),
            context.execution.ready,
            cls._context_level_digest(
                structure.nearest_support
                if structure is not None
                else None
            ),
            cls._context_level_digest(
                structure.nearest_resistance
                if structure is not None
                else None
            ),
        )

    @staticmethod
    def _compact_market_context(
        context: MarketContext,
    ) -> dict:
        flow = context.flow
        liquidity = context.liquidity
        structure = context.structure
        forming = context.forming_candle

        def level_payload(level) -> dict | None:
            if level is None:
                return None
            return {
                "kind": level.kind,
                "center": level.center,
                "low": level.low,
                "high": level.high,
                "timeframe": level.timeframe,
                "score": level.score,
                "generationId": level.generation_id,
                "lifecycle": level.lifecycle,
            }

        return {
            "schemaVersion": 2,
            "compact": True,
            "symbol": context.symbol,
            "observedAtMs": context.observed_at_ms,
            "lastPrice": context.last_price,
            "legacyTrend": context.legacy_trend.value,
            "htfBias": (
                {
                    "bias": context.htf_bias.bias.value,
                    "strength": context.htf_bias.strength,
                    "alignment": context.htf_bias.alignment,
                }
                if context.htf_bias is not None
                else None
            ),
            "localRegime": (
                {
                    "regime": (
                        context.local_regime.regime.value
                    ),
                    "direction": (
                        context.local_regime.direction.value
                    ),
                    "parentDirection": (
                        context.local_regime
                        .parent_direction.value
                    ),
                    "strength": (
                        context.local_regime.strength
                    ),
                    "recentMovePct": (
                        context.local_regime
                        .recent_move_pct
                    ),
                    "rangeExpansionRatio": (
                        context.local_regime
                        .range_expansion_ratio
                    ),
                    "directionalEfficiency": (
                        context.local_regime
                        .directional_efficiency
                    ),
                }
                if context.local_regime is not None
                else None
            ),
            "flowContext": (
                {
                    "dominantDirection": (
                        flow.dominant_direction.value
                    ),
                    "directionalScore": (
                        flow.directional_score
                    ),
                    "coherence": flow.coherence,
                    "longAlignment": {
                        "classification": (
                            flow.long_alignment
                            .classification.value
                        ),
                        "score": flow.long_alignment.score,
                    },
                    "shortAlignment": {
                        "classification": (
                            flow.short_alignment
                            .classification.value
                        ),
                        "score": flow.short_alignment.score,
                    },
                }
                if flow is not None
                else None
            ),
            "liquidityEvidence": (
                {
                    "state": liquidity.state.value,
                    "directionalBias": (
                        liquidity.directional_bias.value
                    ),
                    "directionalStrength": (
                        liquidity.directional_strength
                    ),
                    "wallSide": liquidity.wall_side,
                    "wallPrice": liquidity.wall_price,
                    "absorptionObserved": (
                        liquidity.absorption_observed
                    ),
                    "consuming": liquidity.consuming,
                }
                if liquidity is not None
                else None
            ),
            "structureContext": (
                {
                    "referencePrice": (
                        structure.reference_price
                    ),
                    "levelCount": structure.level_count,
                    "trendlineCount": (
                        structure.trendline_count
                    ),
                    "nearestSupport": level_payload(
                        structure.nearest_support
                    ),
                    "nearestResistance": level_payload(
                        structure.nearest_resistance
                    ),
                    "supportDistancePct": (
                        structure.support_distance_pct
                    ),
                    "resistanceDistancePct": (
                        structure.resistance_distance_pct
                    ),
                }
                if structure is not None
                else None
            ),
            "executionContext": {
                "ready": context.execution.ready,
                "bookFresh": context.execution.book_fresh,
                "candleFresh": (
                    context.execution.candle_fresh
                ),
                "bookAgeSeconds": (
                    context.execution.book_age_seconds
                ),
                "candleAgeSeconds": (
                    context.execution.candle_age_seconds
                ),
                "spreadPct": context.execution.spread_pct,
                "top5DepthUsd": (
                    context.execution.top5_depth_usd
                ),
            },
            "formingCandle": (
                {
                    "startMs": forming.start_ms,
                    "ageSeconds": forming.age_seconds,
                    "progressRatio": forming.progress_ratio,
                    "bodyPct": forming.body_pct,
                    "rangePct": forming.range_pct,
                    "closePosition": forming.close_position,
                    "volumePaceRatio": (
                        forming.volume_pace_ratio
                    ),
                    "rangeExpansionRatio": (
                        forming.range_expansion_ratio
                    ),
                    "velocityBpsPerSecond": (
                        forming.velocity_bps_per_second
                    ),
                    "direction": forming.direction.value,
                    "priceSource": forming.price_source,
                    "volumeSource": forming.volume_source,
                    "tapeUpdates": forming.tape_updates,
                    "lastTradeAgeSeconds": (
                        forming.last_trade_age_seconds
                    ),
                    "klineSnapshotAgeSeconds": (
                        forming.kline_snapshot_age_seconds
                    ),
                    "microMove5sBps": (
                        forming.micro_move_5s_bps
                    ),
                    "microRange5sBps": (
                        forming.micro_range_5s_bps
                    ),
                    "microMove15sBps": (
                        forming.micro_move_15s_bps
                    ),
                    "microRange15sBps": (
                        forming.micro_range_15s_bps
                    ),
                }
                if forming is not None
                else None
            ),
        }

    def _commit_market_context(
        self,
        session: ActiveSymbolSession,
        *,
        observed_at_ms: int,
        emit: bool = True,
    ) -> None:
        context = self._build_market_context(
            session,
            observed_at_ms=observed_at_ms,
        )
        fingerprint = context.fingerprint()
        previous = session.market_context_fingerprint
        semantic_fingerprint = (
            self._market_context_semantic_fingerprint(
                context
            )
        )
        previous_semantic = (
            session.market_context_semantic_fingerprint
        )

        session.market_context = context
        session.market_context_fingerprint = fingerprint
        session.market_context_semantic_fingerprint = (
            semantic_fingerprint
        )

        if not emit or previous == fingerprint:
            return

        semantic_changed = (
            previous_semantic != semantic_fingerprint
        )
        observed_at = observed_at_ms / 1000
        min_interval = max(
            0.0,
            float(
                self.config
                .market_context_event_interval_seconds
            ),
        )
        interval_elapsed = (
            session.last_market_context_event_at <= 0
            or observed_at
            - session.last_market_context_event_at
            >= min_interval
        )

        if not semantic_changed and not interval_elapsed:
            session.market_context_changes_suppressed += 1
            session.market_context_changes_pending += 1
            return

        suppressed = (
            session.market_context_changes_pending
        )
        session.market_context_changes_pending = 0
        session.last_market_context_event_at = observed_at
        session.market_context_events_emitted += 1
        self._emit(
            "market_context_changed",
            session.symbol,
            {
                "previousFingerprint": (
                    list(previous)
                    if previous
                    else None
                ),
                "semanticChanged": semantic_changed,
                "suppressedChangesSinceLastEvent": (
                    suppressed
                ),
                "eventIntervalSeconds": min_interval,
                "marketContext": (
                    self._compact_market_context(context)
                ),
            },
        )

    def _refresh_market_context(
        self,
        session: ActiveSymbolSession,
        *,
        closed_1m: list[Candle],
        closed_5m: list[Candle],
        closed_15m: list[Candle],
        closed_1h: list[Candle],
        commit: bool = True,
        observed_at_ms: int | None = None,
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
        if commit:
            self._commit_market_context(
                session,
                observed_at_ms=(
                    int(time() * 1000)
                    if observed_at_ms is None
                    else observed_at_ms
                ),
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
    def _annotate_decision_context(
        session: ActiveSymbolSession,
        decision: StrategyDecision,
    ) -> None:
        context = session.market_context
        if context is None:
            return

        effective_action = decision.action
        if effective_action == Action.WAIT:
            shadow = str(
                (decision.details or {}).get("shadowAction")
                or ""
            )
            if shadow in {"long", "short"}:
                effective_action = Action(shadow)

        flow_alignment = context.flow_alignment_for(
            effective_action
        )
        liquidity_alignment = context.liquidity_alignment_for(
            effective_action
        )
        structure = context.structure

        decision.details["decisionContext"] = {
            "schemaVersion": 1,
            "marketObservedAtMs": context.observed_at_ms,
            "marketContextFingerprint": list(
                context.fingerprint()
            ),
            "legacyTrend": context.legacy_trend.value,
            "htfBias": (
                context.htf_bias.bias.value
                if context.htf_bias is not None
                else None
            ),
            "localRegime": (
                context.local_regime.regime.value
                if context.local_regime is not None
                else None
            ),
            "localDirection": (
                context.local_regime.direction.value
                if context.local_regime is not None
                else None
            ),
            "flowAlignment": (
                flow_alignment.public()
                if flow_alignment is not None
                else None
            ),
            "liquidityState": (
                context.liquidity.state.value
                if context.liquidity is not None
                else None
            ),
            "liquidityAlignment": (
                liquidity_alignment.public()
                if liquidity_alignment is not None
                else None
            ),
            "entryFreshness": (
                decision.details.get("entryFreshness")
                if isinstance(
                    decision.details.get("entryFreshness"),
                    dict,
                )
                else None
            ),
            "opportunityFreshness": (
                decision.details.get("opportunityFreshness")
                if isinstance(
                    decision.details.get("opportunityFreshness"),
                    dict,
                )
                else None
            ),
            "formingCandle": (
                context.forming_candle.public()
                if context.forming_candle is not None
                else None
            ),
            "analysisRuntime": session.analysis_runtime_public(),
            "executionReady": context.execution.ready,
            "spreadPct": context.execution.spread_pct,
            "top5DepthUsd": (
                context.execution.top5_depth_usd
            ),
            "nearestSupportDistancePct": (
                structure.support_distance_pct
                if structure is not None
                else None
            ),
            "nearestResistanceDistancePct": (
                structure.resistance_distance_pct
                if structure is not None
                else None
            ),
        }

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

        details = decision.details or {}
        opportunity_arm = details.get("opportunityArm")
        trigger_state = state in ENTRY_FRESHNESS_TRIGGER_STATES.get(
            strategy,
            set(),
        )
        if trigger_state or isinstance(opportunity_arm, dict):
            if anchor is None or anchor.get("objectKey") != object_key:
                arm_price = (
                    opportunity_arm.get("price")
                    if isinstance(opportunity_arm, dict)
                    else None
                )
                trigger_price = (
                    float(arm_price)
                    if isinstance(arm_price, (int, float)) and arm_price > 0
                    else (
                        session.orderbook.mid
                        or session.last_price
                        or decision.watched_level
                    )
                )
                arm_ms = (
                    opportunity_arm.get("observedAtMs")
                    if isinstance(opportunity_arm, dict)
                    else None
                )
                trigger_ts = (
                    float(arm_ms) / 1000
                    if isinstance(arm_ms, (int, float)) and arm_ms > 0
                    else float(observed_at)
                )
                arm_source = (
                    str(opportunity_arm.get("source") or "opportunity_arm")
                    if isinstance(opportunity_arm, dict)
                    else f"{state}_state"
                )
                if trigger_price and trigger_price > 0:
                    anchor = {
                        "objectKey": object_key,
                        "triggerPrice": float(trigger_price),
                        "triggerTs": trigger_ts,
                        "source": arm_source,
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
        freshness_public = freshness.public()
        decision.details["opportunityFreshness"] = freshness_public
        decision.details["entryFreshness"] = freshness_public
        if (
            decision.tradeable
            and freshness.confirmation_age_seconds is not None
        ):
            decision.details["armToFireSeconds"] = (
                freshness.confirmation_age_seconds
            )
            decision.details["causalTriggerSource"] = freshness.source

        fingerprint = (
            object_key,
            freshness.classification.value,
            (
                round(float(freshness.effective_spent_ratio), 1)
                if freshness.effective_spent_ratio is not None
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
                "entryFreshness": freshness_public,
                "opportunityFreshness": freshness_public,
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

    def _build_risk_plan_for_opportunity(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
        setup_id: str,
        position_action: str,
        existing_position: Position | None,
    ):
        return self.risk.build_plan(
            session.symbol,
            decision,
            self.broker.balance,
            session.orderbook,
            self.broker.available_notional,
            self.broker.available_risk_usd,
            setup_id=setup_id,
            existing_position_notional=(
                existing_position.notional
                if position_action == "add"
                and existing_position is not None
                else 0.0
            ),
            existing_position_all_in_risk_usd=(
                existing_position.all_in_risk_usd(
                    self.config
                )
                if position_action == "add"
                and existing_position is not None
                else 0.0
            ),
        )

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
        candidate_map = {
            item.symbol: item
            for item in self.candidates
        }

        for session in self.sessions.values():
            if session.symbol in self.broker.pending_entries:
                continue
            existing_position = self.broker.positions.get(
                session.symbol
            )
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

            planned: list[tuple[
                StrategyDecision,
                object,
                SemanticCandidateAssessment,
                int,
                float,
                str,
            ]] = []

            for decision in session.decisions.values():
                if decision.strategy == "orderbook_density":
                    continue
                if (
                    not decision.tradeable
                    or not self.strategy_enabled.get(
                        decision.strategy,
                        False,
                    )
                ):
                    continue

                setup_id = self._resolve_setup_id(
                    session,
                    decision,
                )
                decision.setup_id = setup_id
                staged_entry = (
                    decision.details.get("stagedEntry")
                    if isinstance(
                        decision.details.get("stagedEntry"),
                        dict,
                    )
                    else {}
                )
                staged_phase = str(
                    staged_entry.get("phase") or "full"
                )
                position_action = (
                    "add"
                    if existing_position is not None
                    else "open"
                )
                if existing_position is not None:
                    if staged_phase != "add":
                        continue
                    if (
                        existing_position.strategy
                        != decision.strategy
                        or existing_position.side
                        != decision.side
                        or existing_position.setup_id
                        != setup_id
                    ):
                        continue
                elif staged_phase == "add":
                    # The probe may have been rejected or timed out. Never
                    # execute an orphaned add as if a position existed.
                    continue

                base_assessment = assess_candidate(
                    decision,
                    session.market_context,
                    partial_take_at_r=(
                        self.config.partial_take_at_r
                    ),
                    partial_take_enabled=(
                        self.config.partial_take_enabled
                    ),
                )
                decision.details["semanticArbitration"] = (
                    base_assessment.public()
                )
                decision.details["riskScale"] = (
                    base_assessment.risk_scale
                )
                decision.details["riskScaleSource"] = (
                    "semantic_arbiter_stage13"
                )
                if not base_assessment.allowed:
                    self._record_arbiter_blocked(
                        session,
                        decision,
                        base_assessment,
                    )
                    continue

                policy_pre = self._evaluate_research_policy(
                    session,
                    decision,
                    phase="pre_plan",
                    arbitration=base_assessment,
                )
                if policy_pre.blocked:
                    continue

                blocked_reason = self._setup_blocked_reason(
                    session,
                    decision.strategy,
                    setup_id,
                    now,
                )
                if blocked_reason:
                    self._record_setup_blocked(
                        session,
                        decision,
                        blocked_reason,
                    )
                    continue

                expectancy_snapshot = self.expectancy.snapshot(
                    decision.strategy,
                    min_samples=(
                        self.config.strategy_expectancy_min_samples
                    ),
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
                            "semanticArbitration": (
                                base_assessment.public()
                            ),
                        },
                    )
                    continue

                if position_action == "add":
                    allowed = (
                        existing_position is not None
                        and not existing_position.partial_taken
                        and self.broker.available_notional > 0
                        and self.broker.available_risk_usd > 0
                    )
                    portfolio_reason = (
                        "allowed"
                        if allowed
                        else "staged add portfolio budget exhausted"
                    )
                else:
                    allowed, portfolio_reason = (
                        self.broker.can_open(session.symbol)
                    )
                if not allowed:
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        portfolio_reason,
                        diagnostics={
                            "semanticArbitration": (
                                base_assessment.public()
                            ),
                        },
                    )
                    continue

                result = self._build_risk_plan_for_opportunity(
                    session,
                    decision,
                    setup_id,
                    position_action,
                    existing_position,
                )
                if not result.allowed or result.plan is None:
                    diagnostics = dict(
                        result.diagnostics or {}
                    )
                    diagnostics["semanticArbitration"] = (
                        base_assessment.public()
                    )
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        result.reason,
                        diagnostics=diagnostics,
                    )
                    continue

                # RiskEngine knows whether a 1R partial is actually
                # economically executable. Feed that lifecycle back into the
                # semantic path check before any order can be selected. If the
                # longer real path exposes a new structural obstacle and
                # reduces riskScale, rebuild the plan at the stricter size.
                post_plan_assessment = base_assessment
                post_plan_blocked = False
                for _ in range(3):
                    economics = (
                        result.plan.strategy_details.get(
                            "economics"
                        )
                        if isinstance(
                            result.plan.strategy_details,
                            dict,
                        )
                        else None
                    )
                    planned_partial = (
                        bool(economics.get("partialPlanned"))
                        if isinstance(economics, dict)
                        and "partialPlanned" in economics
                        else self.config.partial_take_enabled
                    )
                    decision.details[
                        "plannedPartialEnabled"
                    ] = planned_partial
                    if isinstance(economics, dict):
                        decision.details[
                            "plannedFirstTakeMovePct"
                        ] = economics.get(
                            "firstTakeMovePct"
                        )

                    revised = assess_candidate(
                        decision,
                        session.market_context,
                        partial_take_at_r=(
                            self.config.partial_take_at_r
                        ),
                        partial_take_enabled=planned_partial,
                    )
                    if not revised.allowed:
                        self._record_arbiter_blocked(
                            session,
                            decision,
                            revised,
                        )
                        post_plan_assessment = revised
                        post_plan_blocked = True
                        break

                    current_scale = float(
                        decision.details.get(
                            "riskScale",
                            1.0,
                        )
                    )
                    stricter_scale = min(
                        current_scale,
                        revised.risk_scale,
                    )
                    post_plan_assessment = revised
                    if stricter_scale >= current_scale - 1e-9:
                        break

                    decision.details["riskScale"] = (
                        stricter_scale
                    )
                    decision.details["riskScaleSource"] = (
                        "semantic_arbiter_post_economics"
                    )
                    result = (
                        self._build_risk_plan_for_opportunity(
                            session,
                            decision,
                            setup_id,
                            position_action,
                            existing_position,
                        )
                    )
                    if (
                        not result.allowed
                        or result.plan is None
                    ):
                        diagnostics = dict(
                            result.diagnostics or {}
                        )
                        diagnostics[
                            "semanticArbitration"
                        ] = revised.public()
                        self._risk_reject_if_changed(
                            session,
                            decision,
                            result.reason,
                            diagnostics=diagnostics,
                        )
                        post_plan_blocked = True
                        break

                if post_plan_blocked or result.plan is None:
                    continue
                base_assessment = post_plan_assessment
                decision.details["semanticArbitration"] = (
                    base_assessment.public()
                )

                if position_action == "add":
                    allowed, add_reason = self.broker.can_add(
                        result.plan
                    )
                    if not allowed:
                        self._risk_reject_if_changed(
                            session,
                            decision,
                            add_reason,
                            diagnostics={
                                "semanticArbitration": (
                                    base_assessment.public()
                                ),
                            },
                        )
                        continue

                economics = (
                    result.plan.strategy_details.get(
                        "economics"
                    )
                    if isinstance(
                        result.plan.strategy_details,
                        dict,
                    )
                    else None
                )
                shadow_reasons = (
                    tuple(
                        economics.get(
                            "shadowRejectReasons"
                        )
                        or []
                    )
                    if isinstance(economics, dict)
                    else ()
                )
                shadow_fingerprint = (
                    decision.strategy,
                    setup_id,
                    shadow_reasons,
                )
                if shadow_reasons:
                    if (
                        session.last_economic_shadow_fingerprint
                        != shadow_fingerprint
                    ):
                        session.last_economic_shadow_fingerprint = (
                            shadow_fingerprint
                        )
                        self._emit(
                            "economic_shadow",
                            session.symbol,
                            {
                                "strategy": decision.strategy,
                                "setupId": setup_id,
                                "shadowRejectReasons": list(
                                    shadow_reasons
                                ),
                                "economics": economics,
                                "decision": decision.public(),
                                "semanticArbitration": (
                                    base_assessment.public()
                                ),
                            },
                            snapshot=True,
                        )
                else:
                    session.last_economic_shadow_fingerprint = (
                        None
                    )

                policy_post = self._evaluate_research_policy(
                    session,
                    decision,
                    phase="post_plan",
                    plan=result.plan,
                    arbitration=base_assessment,
                )
                if policy_post.blocked:
                    continue

                scanner_candidate = candidate_map.get(
                    session.symbol
                )
                activity_rank = (
                    scanner_candidate.activity_rank
                    if (
                        scanner_candidate
                        and scanner_candidate.activity_rank
                    )
                    else 99
                )
                activity_score = (
                    scanner_candidate.activity_score
                    if scanner_candidate
                    else 0.0
                )
                planned.append(
                    (
                        decision,
                        result.plan,
                        base_assessment,
                        int(activity_rank),
                        float(activity_score),
                        position_action,
                    )
                )

            if not planned:
                continue

            final_assessments = assess_session_candidates(
                [row[0] for row in planned],
                session.market_context,
                partial_take_at_r=(
                    self.config.partial_take_at_r
                ),
                partial_take_enabled=(
                    self.config.partial_take_enabled
                ),
            )
            for (
                decision,
                plan,
                base_assessment,
                activity_rank,
                activity_score,
                position_action,
            ) in planned:
                assessment = final_assessments.get(
                    decision.strategy,
                    base_assessment,
                )
                decision.details["semanticArbitration"] = (
                    assessment.public()
                )
                if not assessment.allowed:
                    self._record_arbiter_blocked(
                        session,
                        decision,
                        assessment,
                    )
                    continue

                policy_final = self._evaluate_research_policy(
                    session,
                    decision,
                    phase="final",
                    plan=plan,
                    arbitration=assessment,
                )
                if policy_final.blocked:
                    continue

                session.arbiter_block_fingerprints.pop(
                    decision.strategy,
                    None,
                )
                plan.strategy_details[
                    "semanticArbitration"
                ] = assessment.public()
                priority = build_selection_priority(
                    assessment,
                    plan,
                    activity_rank=activity_rank,
                    activity_score=activity_score,
                )
                plan.strategy_details[
                    "selectionPriority"
                ] = priority.public()
                opportunities.append(
                    Opportunity(
                        priority=priority,
                        arbitration=assessment,
                        session=session,
                        decision=decision,
                        plan=plan,
                        position_action=position_action,
                    )
                )

        if not opportunities:
            return

        best = max(
            opportunities,
            key=lambda item: item.priority.key(),
        )
        if best.position_action == "add":
            allowed, reason = self.broker.can_add(
                best.plan
            )
        else:
            allowed, reason = self.broker.can_open(
                best.session.symbol
            )
        if not allowed:
            self._risk_reject_if_changed(
                best.session,
                best.decision,
                reason,
                diagnostics={
                    "semanticArbitration": (
                        best.arbitration.public()
                    ),
                    "selectionPriority": (
                        best.priority.public()
                    ),
                },
            )
            return

        best.session.last_risk_fingerprint = None
        best.session.last_blocked_fingerprint = None
        selection_payload = {
            "semanticArbitration": (
                best.arbitration.public()
            ),
            "selectionPriority": best.priority.public(),
            "playbookSetupQuality": float(
                best.decision.details.get(
                    "setupQuality",
                    best.decision.confidence,
                )
                or 0.0
            ),
        }

        if best.plan.entry_mode == "maker_limit":
            if best.position_action == "add":
                pending = self.broker.place_pending_add(
                    best.plan,
                    min_trade_ts_ms=(
                        best.session.trades[-1].ts_ms
                        if best.session.trades
                        else None
                    ),
                )
                pending_event = "entry_add_pending"
            else:
                pending = self.broker.place_pending(
                    best.plan,
                    min_trade_ts_ms=(
                        best.session.trades[-1].ts_ms
                        if best.session.trades
                        else None
                    ),
                )
                pending_event = "entry_pending"
            self._emit(
                pending_event,
                best.session.symbol,
                {
                    "plan": best.plan.public(),
                    "pending": pending.public(),
                    "reasons": best.decision.reasons,
                    "visuals": best.decision.visuals,
                    **selection_payload,
                },
                snapshot=True,
            )
            return

        best.session.last_trade_at = now
        if best.position_action == "add":
            position = self.broker.add(
                best.plan,
                best.session.orderbook,
            )
            self._record_added_position(
                best.session,
                best.decision,
                best.plan.public(),
                position.public(),
                semantic_arbitration=(
                    best.arbitration.public()
                ),
                selection_priority=best.priority.public(),
            )
        else:
            position = self.broker.open(
                best.plan,
                best.session.orderbook,
            )
            self._record_opened_position(
                best.session,
                best.decision,
                best.plan.public(),
                position.public(),
                semantic_arbitration=(
                    best.arbitration.public()
                ),
                selection_priority=best.priority.public(),
            )

    def _record_opened_position(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision | None,
        plan: dict,
        position: dict,
        *,
        semantic_arbitration: dict | None = None,
        selection_priority: dict | None = None,
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
                "semanticArbitration": (
                    semantic_arbitration
                    or (
                        plan.get("strategy_details", {})
                        .get("semanticArbitration")
                        if isinstance(
                            plan.get("strategy_details"),
                            dict,
                        )
                        else None
                    )
                ),
                "selectionPriority": (
                    selection_priority
                    or (
                        plan.get("strategy_details", {})
                        .get("selectionPriority")
                        if isinstance(
                            plan.get("strategy_details"),
                            dict,
                        )
                        else None
                    )
                ),
                "playbookSetupQuality": float(
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

    def _record_added_position(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
        plan: dict,
        position: dict,
        *,
        semantic_arbitration: dict | None = None,
        selection_priority: dict | None = None,
    ) -> None:
        strategy_key = str(plan.get("strategy") or "")
        stats = self.strategy_stats.get(strategy_key)
        if stats is not None:
            stats["positionAdds"] = int(
                stats.get("positionAdds") or 0
            ) + 1
        strategy = self.strategies.get(strategy_key)
        if strategy is not None:
            strategy.mark_opened(session.symbol, decision)
        session.last_trade_at = time()
        self._emit(
            "position_added",
            session.symbol,
            {
                "plan": plan,
                "position": position,
                "reasons": decision.reasons,
                "visuals": decision.visuals,
                "semanticArbitration": (
                    semantic_arbitration
                    or (
                        plan.get("strategy_details", {})
                        .get("semanticArbitration")
                        if isinstance(
                            plan.get("strategy_details"),
                            dict,
                        )
                        else None
                    )
                ),
                "selectionPriority": (
                    selection_priority
                    or (
                        plan.get("strategy_details", {})
                        .get("selectionPriority")
                        if isinstance(
                            plan.get("strategy_details"),
                            dict,
                        )
                        else None
                    )
                ),
                "stagedEntry": decision.details.get(
                    "stagedEntry"
                ),
            },
            snapshot=True,
        )

    def _record_arbiter_blocked(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
        assessment: SemanticCandidateAssessment,
    ) -> None:
        fingerprint = (
            decision.setup_id,
            tuple(assessment.blockers),
            tuple(assessment.conflicting_strategies),
            assessment.structural_path.blocked,
        )
        if (
            session.arbiter_block_fingerprints.get(
                decision.strategy
            )
            == fingerprint
        ):
            return
        session.arbiter_block_fingerprints[
            decision.strategy
        ] = fingerprint
        self._emit(
            "arbiter_blocked",
            session.symbol,
            {
                "strategy": decision.strategy,
                "setupId": decision.setup_id,
                "blockers": list(assessment.blockers),
                "conflictingStrategies": list(
                    assessment.conflicting_strategies
                ),
                "confluenceStrategies": list(
                    assessment.confluence_strategies
                ),
                "semanticArbitration": assessment.public(),
                "decision": decision.public(),
            },
            snapshot=True,
        )

    @staticmethod
    def _attach_research_policy_assessment(
        decision: StrategyDecision,
        assessment: PolicyAssessment,
    ) -> None:
        if not assessment.would_block:
            return
        existing = (
            decision.details.get(
                "researchPolicyAssessments"
            )
            if isinstance(decision.details, dict)
            else None
        )
        rows = [
            row
            for row in (
                existing
                if isinstance(existing, list)
                else []
            )
            if not (
                isinstance(row, dict)
                and row.get("phase")
                == assessment.phase
            )
        ]
        rows.append(assessment.public())
        decision.details[
            "researchPolicyAssessments"
        ] = rows

    @staticmethod
    def _attach_plan_policy_assessment(
        plan,
        assessment: PolicyAssessment,
    ) -> None:
        if not assessment.would_block:
            return
        details = plan.strategy_details
        existing = (
            details.get(
                "researchPolicyAssessments"
            )
            if isinstance(details, dict)
            else None
        )
        rows = [
            row
            for row in (
                existing
                if isinstance(existing, list)
                else []
            )
            if not (
                isinstance(row, dict)
                and row.get("phase")
                == assessment.phase
            )
        ]
        rows.append(assessment.public())
        details["researchPolicyAssessments"] = rows

    def _evaluate_research_policy(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
        *,
        phase: str,
        plan=None,
        arbitration: (
            SemanticCandidateAssessment
            | dict
            | None
        ) = None,
    ) -> PolicyAssessment:
        assessment = self.research_policy.evaluate(
            decision,
            session.market_context,
            phase=phase,
            plan=plan,
            arbitration=arbitration,
        )
        if not assessment.would_block:
            return assessment

        self._attach_research_policy_assessment(
            decision,
            assessment,
        )
        if plan is not None:
            self._attach_plan_policy_assessment(
                plan,
                assessment,
            )

        fingerprint = (
            decision.setup_id,
            assessment.policy_id,
            assessment.policy_version,
            assessment.phase,
            assessment.mode.value,
            assessment.matched_rule_ids,
            assessment.blocked,
        )
        key = f"{decision.strategy}:{phase}"
        if (
            session.research_policy_fingerprints.get(
                key
            )
            != fingerprint
        ):
            session.research_policy_fingerprints[
                key
            ] = fingerprint
            event = (
                "research_policy_blocked"
                if assessment.blocked
                else "research_policy_shadow"
            )
            stats = self.strategy_stats.get(
                decision.strategy
            )
            if isinstance(stats, dict):
                counter = (
                    "researchPolicyBlockedMatches"
                    if assessment.blocked
                    else "researchPolicyShadowMatches"
                )
                stats[counter] = int(
                    stats.get(counter) or 0
                ) + 1
            self._emit(
                event,
                session.symbol,
                {
                    "strategy": decision.strategy,
                    "setupId": decision.setup_id,
                    "assessment": assessment.public(),
                    "policy": self.research_policy.public(),
                    "decision": decision.public(),
                },
                snapshot=True,
            )
        return assessment

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

    def _validate_pending_entry(
        self,
        session: ActiveSymbolSession,
    ) -> None:
        pending = self.broker.pending_entries.get(
            session.symbol
        )
        if pending is None:
            return

        strategy_key = pending.plan.strategy
        decision = session.decisions.get(strategy_key)
        reason: str | None = None

        if not self.strategy_enabled.get(strategy_key, False):
            reason = "strategy_disabled"
        elif decision is None:
            reason = "decision_missing"
        elif not decision.tradeable:
            reason = "setup_no_longer_tradeable"
        elif decision.side != pending.plan.side:
            reason = "setup_direction_changed"
        else:
            current_setup_id = self._resolve_setup_id(
                session,
                decision,
            )
            if current_setup_id != pending.plan.setup_id:
                reason = "setup_identity_changed"

            freshness = (
                decision.details.get("opportunityFreshness")
                if isinstance(decision.details, dict)
                else None
            )
            freshness_class = (
                str(freshness.get("classification") or "")
                if isinstance(freshness, dict)
                else ""
            )
            if (
                reason is None
                and freshness_class in {"late", "exhausted"}
            ):
                reason = (
                    "setup_freshness_"
                    + freshness_class
                )

            entry_context = (
                decision.details.get(
                    "entryContextAssessment"
                )
                if isinstance(decision.details, dict)
                else None
            )
            if (
                reason is None
                and isinstance(entry_context, dict)
                and entry_context.get("allowed") is False
            ):
                reason = "entry_context_invalidated"

        if reason is None:
            return

        event = self.broker.cancel_pending(
            session.symbol,
            f"setup_invalidated:{reason}",
        )
        if event is not None:
            self._emit(
                "entry_cancelled",
                session.symbol,
                event,
                snapshot=True,
            )

    def _maybe_strategy_invalidation(
        self,
        session: ActiveSymbolSession,
    ) -> None:
        pos = self.broker.positions.get(session.symbol)
        if pos is None:
            return
        if (
            time() - pos.opened_at
            < max(
                0.0,
                self.config.strategy_invalidation_grace_seconds,
            )
        ):
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
            market_context=session.market_context,
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
        trade_price: float | None = None,
    ) -> None:
        resolved_trade_price = (
            float(trade_price)
            if isinstance(trade_price, (int, float))
            else session.last_price
        )
        pending_events = self.broker.mark_pending(
            session.symbol,
            resolved_trade_price,
            trade_ts_ms=trade_ts_ms,
        )
        for event in pending_events:
            if event.get("event") in {
                "entry_filled",
                "entry_added",
            }:
                is_add = event.get("event") == "entry_added"
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
                if is_add:
                    self._record_added_position(
                        session,
                        decision,
                        plan,
                        dict(event.get("position") or {}),
                    )
                else:
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
        self._mark_position_from_book(
            session,
            trade_price=resolved_trade_price,
        )

    def _mark_position_from_book(
        self,
        session: ActiveSymbolSession,
        *,
        trade_price: float | None = None,
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
            trade_price=trade_price,
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
                    self._update_side_regime_stats(
                        stats,
                        event,
                    )
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

    @staticmethod
    def _update_side_regime_stats(
        stats: dict[str, object],
        event: dict,
    ) -> None:
        side = str(event.get("side") or "unknown")
        if side not in {"long", "short"}:
            return

        details = event.get("strategyDetails") or {}
        decision_context = (
            details.get("decisionContext")
            if isinstance(details, dict)
            else None
        )
        regime = (
            str(
                decision_context.get("localRegime")
                or "unknown"
            )
            if isinstance(decision_context, dict)
            else "unknown"
        )

        side_regime = stats.setdefault(
            "sideRegime",
            {"long": {}, "short": {}},
        )
        if not isinstance(side_regime, dict):
            return
        side_bucket = side_regime.setdefault(side, {})
        if not isinstance(side_bucket, dict):
            return

        def update_bucket(key: str) -> None:
            bucket = side_bucket.setdefault(
                key,
                {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "grossPnl": 0.0,
                    "fees": 0.0,
                    "netPnl": 0.0,
                    "mfeRTotal": 0.0,
                    "mfeRSamples": 0,
                    "maeRTotal": 0.0,
                    "maeRSamples": 0,
                },
            )
            if not isinstance(bucket, dict):
                return
            net = float(event.get("netPnl") or 0.0)
            bucket["trades"] = int(
                bucket.get("trades") or 0
            ) + 1
            if net > 0:
                bucket["wins"] = int(
                    bucket.get("wins") or 0
                ) + 1
            elif net < 0:
                bucket["losses"] = int(
                    bucket.get("losses") or 0
                ) + 1
            bucket["grossPnl"] = float(
                bucket.get("grossPnl") or 0.0
            ) + float(event.get("grossPnl") or 0.0)
            bucket["fees"] = float(
                bucket.get("fees") or 0.0
            ) + float(event.get("fees") or 0.0)
            bucket["netPnl"] = float(
                bucket.get("netPnl") or 0.0
            ) + net

            mfe_r = event.get("mfeR")
            if isinstance(mfe_r, (int, float)):
                bucket["mfeRTotal"] = float(
                    bucket.get("mfeRTotal") or 0.0
                ) + float(mfe_r)
                bucket["mfeRSamples"] = int(
                    bucket.get("mfeRSamples") or 0
                ) + 1
            mae_r = event.get("maeR")
            if isinstance(mae_r, (int, float)):
                bucket["maeRTotal"] = float(
                    bucket.get("maeRTotal") or 0.0
                ) + float(mae_r)
                bucket["maeRSamples"] = int(
                    bucket.get("maeRSamples") or 0
                ) + 1

            trades = int(bucket.get("trades") or 0)
            wins = int(bucket.get("wins") or 0)
            bucket["winRate"] = (
                wins / trades
                if trades > 0
                else None
            )
            mfe_samples = int(
                bucket.get("mfeRSamples") or 0
            )
            mae_samples = int(
                bucket.get("maeRSamples") or 0
            )
            bucket["averageMfeR"] = (
                float(bucket.get("mfeRTotal") or 0.0)
                / mfe_samples
                if mfe_samples > 0
                else None
            )
            bucket["averageMaeR"] = (
                float(bucket.get("maeRTotal") or 0.0)
                / mae_samples
                if mae_samples > 0
                else None
            )

        update_bucket("all")
        update_bucket(regime)

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

    def _record_decision_if_changed(
        self,
        session: ActiveSymbolSession,
        decision: StrategyDecision,
    ) -> None:
        fingerprint = (
            decision.action.value,
            decision.setup_id,
            round(decision.watched_level or 0, 8),
            round(decision.entry or 0, 8),
            decision.details.get("state"),
            tuple(decision.reasons),
        )
        if (
            session.decision_fingerprints.get(decision.strategy)
            == fingerprint
        ):
            return

        observed_at = time()
        state = str(decision.details.get("state") or "")
        if state:
            previous_state = session.strategy_states.get(
                decision.strategy
            )
            previous_started_at = (
                session.strategy_state_started_at.get(
                    decision.strategy
                )
            )
            if previous_state != state:
                previous_duration = (
                    max(0.0, observed_at - previous_started_at)
                    if previous_started_at is not None
                    else None
                )
                state_timing = {
                    "fromState": previous_state,
                    "toState": state,
                    "transitionAt": observed_at,
                    "stateEnteredAt": observed_at,
                    "stateAgeSeconds": 0.0,
                    "previousStateDurationSeconds": (
                        previous_duration
                    ),
                }
                session.strategy_states[decision.strategy] = state
                session.strategy_state_started_at[
                    decision.strategy
                ] = observed_at
                decision.details["stateTiming"] = state_timing
                self._emit(
                    "strategy_state_transition",
                    session.symbol,
                    {
                        "strategy": decision.strategy,
                        "setupId": decision.setup_id,
                        "action": decision.action.value,
                        "tradeable": decision.tradeable,
                        **state_timing,
                        "opportunityArm": (
                            decision.details.get(
                                "opportunityArm"
                            )
                        ),
                        "preparedOpportunity": (
                            decision.details.get(
                                "preparedOpportunity"
                            )
                        ),
                        "fireTrigger": (
                            decision.details.get(
                                "fireTrigger"
                            )
                        ),
                        "opportunityFreshness": (
                            decision.details.get(
                                "opportunityFreshness"
                            )
                        ),
                        "formingCandle": (
                            session.forming_candle_context.public()
                            if (
                                session.forming_candle_context
                                is not None
                            )
                            else None
                        ),
                    },
                )
            else:
                state_started_at = (
                    previous_started_at
                    if previous_started_at is not None
                    else observed_at
                )
                session.strategy_state_started_at.setdefault(
                    decision.strategy,
                    state_started_at,
                )
                decision.details["stateTiming"] = {
                    "fromState": state,
                    "toState": state,
                    "transitionAt": None,
                    "stateEnteredAt": state_started_at,
                    "stateAgeSeconds": max(
                        0.0,
                        observed_at - state_started_at,
                    ),
                    "previousStateDurationSeconds": None,
                }

        session.decision_fingerprints[decision.strategy] = (
            fingerprint
        )
        stats = self.strategy_stats.get(decision.strategy)
        if stats is not None:
            stats["decisions"] = int(stats["decisions"]) + 1
            stats["decisionUpdates"] = (
                int(stats["decisionUpdates"]) + 1
            )
            state_counts = stats.get("stateCounts")
            if isinstance(state_counts, dict):
                state_key = str(
                    decision.details.get("state") or "unknown"
                )
                state_counts[state_key] = (
                    int(state_counts.get(state_key, 0)) + 1
                )
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
        observed_at_ms = int(observed_at * 1000)
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
