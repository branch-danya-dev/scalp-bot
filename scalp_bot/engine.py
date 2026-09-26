from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .bybit import (
    BybitRestClient,
    MarketMessage,
    OrderBookSequenceError,
    OrderBookState,
    stream_symbol,
)
from .config import Settings
from .runtime_clock import RuntimeClock, SystemRuntimeClock, RecordingRuntimeClock
from .input_scope import InputScopes, input_scope
from .input_journal import InputJournal
from .source_failure import SourceFailure, describe_source_error
from .source_await import source_await
from .manifest_validation import fingerprint
from .domain import Action, Candle, Candidate, OrderBook, Side, StrategyDecision, TradeTick, Trend
from .paper import PaperBroker, Position
from .expectancy import StrategyExpectancyBook
from .strategy_policy import minimum_expectancy_r
from .observability import build_decision_trace
from .instrument import InstrumentSpec
from .latency_observability import (
    configure_telemetry,
    exchange_receive_seconds,
    latency_metrics_snapshot,
    latency_snapshot,
    observe_latency,
    observe_recorder_health,
    span,
    stream_name,
)
from .recorder import SessionRecorder
from .run_manifest import build_run_manifest, code_provenance
from .market_clock import MarketClock, receipt_age_seconds
from .research_policy import (
    PolicyAssessment,
    PolicyMode,
    ResearchPolicyRuntime,
)
from .risk import RiskEngine
from .scenario import ScenarioRouter
from .execution_book import coherent_execution_book
from .scenario_runtime import ScenarioRuntime
from .strategy.flow import best_level_ofi_usd, prune_trades
from .strategy.lifecycle import LevelLifecycleTracker
from .strategy.structure import aggregate_candles
from .strategy import (
    create_default_strategies,
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
    clock: RuntimeClock = field(default_factory=SystemRuntimeClock, kw_only=True, repr=False, compare=False)
    candles: list[Candle] = field(default_factory=list)
    instrument: InstrumentSpec | None = None
    fee_schedule: FeeSchedule | None = None
    mark_price: float = 0.0
    funding_rate: float | None = None
    next_funding_time_ms: int | None = None
    context_5m: list[Candle] = field(default_factory=list)
    context_15m: list[Candle] = field(default_factory=list)
    context_1h: list[Candle] = field(default_factory=list)
    # orderbook is the latency-sensitive L50 book kept under the legacy name
    # for compatibility with existing strategy/test code.
    orderbook: OrderBook = field(default_factory=OrderBook)
    deep_orderbook: OrderBook = field(default_factory=OrderBook)
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
    trade_sequence: int = 0
    last_research_trade_sequence: int = 0
    last_replay_trade_sequence: int = 0
    book_flow: deque[tuple[int, float]] = field(default_factory=deque)
    last_book_flow_ms: int = 0
    level_tracker: LevelLifecycleTracker = field(default_factory=LevelLifecycleTracker)
    consumed_setups: dict[str, str] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    nontradeable_since: dict[str, float] = field(default_factory=dict)
    activated_at: float | None = None
    last_ranked_at: float | None = None
    last_signal_at: float = 0.0
    last_trade_at: float = 0.0
    last_market_at: float = 0.0
    last_book_at: float = 0.0
    last_deep_book_at: float = 0.0
    book_stale_after_seconds: float = 1.5
    deep_book_stale_after_seconds: float = 1.5
    confirmed_candle_stale_after_seconds: float = 150.0
    book_synced: bool | None = None
    deep_book_synced: bool | None = None
    fast_book_seq: int | None = None
    deep_book_seq: int | None = None
    fast_book_exchange_ts_ms: int = 0
    deep_book_exchange_ts_ms: int = 0
    deep_book_max_skew_seconds: float = 0.50
    last_trade_stream_at: float = 0.0
    receipt_clock_required: bool = False
    fast_receipt_mono: float | None = None
    deep_receipt_mono: float | None = None
    trade_receipt_mono: float | None = None
    latest_processed_event_ms: int = 0
    trade_exchange_ts_ms: int = 0
    clock_reading: dict | None = None
    clock_block_reason: str | None = "unobserved"
    last_kline_at: float = 0.0
    last_eval: float = 0.0
    last_event_eval_at: float = 0.0
    event_eval_pending: bool = False
    event_eval_capture_id: int | None = None
    event_eval_owner: object | None = field(default=None, repr=False, compare=False)
    pending_latency_message: MarketMessage | None = None
    fast_event_requests: int = 0
    fast_event_evaluations: int = 0
    fast_event_coalesced: int = 0
    last_fast_event_reason: str | None = None
    last_fast_event_at_ms: int = 0
    fast_market_queue_depth: int = 0
    fast_market_queue_lag_ms: float = 0.0
    fast_market_queue_max_lag_ms: float = 0.0
    deep_market_queue_depth: int = 0
    deep_market_queue_lag_ms: float = 0.0
    deep_market_queue_max_lag_ms: float = 0.0
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

    scenario_view: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.activated_at is None:
            self.activated_at = self.clock.time()
        if self.last_ranked_at is None:
            self.last_ranked_at = self.clock.time()

    def funding_public(self) -> dict:
        return {
            "markPrice": self.mark_price or None,
            "fundingRate": self.funding_rate,
            "nextFundingTimeMs": self.next_funding_time_ms,
            "source": "bybit_linear_ticker",
            "estimatedPaperSettlement": True,
        }

    def book_age_seconds(self, now: float | None = None) -> float | None:
        if self.receipt_clock_required:
            return None if self.fast_receipt_mono is None else receipt_age_seconds(
                now_mono=self.clock.perf_counter_ns() / 1e9, received_mono=self.fast_receipt_mono)
        if self.last_book_at <= 0:
            return None
        resolved_now = self.clock.time() if now is None else now
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

    def depth_orderbook(self) -> OrderBook:
        # Manually-constructed sessions in deterministic tests/replay predate
        # the dual-book runtime. Live sessions explicitly start deep sync as
        # False, so only legacy/test sessions may fall back to the fast book.
        if (
            self.deep_orderbook.bids
            and self.deep_orderbook.asks
        ):
            return self.deep_orderbook
        if self.deep_book_synced is None:
            return self.orderbook
        return self.deep_orderbook

    def deep_book_age_seconds(
        self,
        now: float | None = None,
    ) -> float | None:
        if self.receipt_clock_required:
            return None if self.deep_receipt_mono is None else receipt_age_seconds(
                now_mono=self.clock.perf_counter_ns() / 1e9, received_mono=self.deep_receipt_mono)
        if self.last_deep_book_at <= 0:
            return None
        resolved_now = self.clock.time() if now is None else now
        return max(
            0.0,
            resolved_now - self.last_deep_book_at,
        )

    def deep_book_skew_seconds(self) -> float | None:
        if (
            self.fast_book_exchange_ts_ms <= 0
            or self.deep_book_exchange_ts_ms <= 0
        ):
            return None
        return max(
            0.0,
            (
                self.fast_book_exchange_ts_ms
                - self.deep_book_exchange_ts_ms
            )
            / 1000.0,
        )

    def deep_book_is_coherent_with_fast(self) -> bool:
        skew = self.deep_book_skew_seconds()
        if skew is None:
            return True
        return skew <= max(
            0.0,
            self.deep_book_max_skew_seconds,
        )

    def deep_book_is_fresh(
        self,
        now: float | None = None,
    ) -> bool:
        if (
            self.deep_book_synced is None
            and not self.deep_orderbook.bids
            and not self.deep_orderbook.asks
        ):
            return self.book_is_fresh(now)
        age = self.deep_book_age_seconds(now)
        if age is None:
            return False
        if self.deep_book_synced is False:
            return False
        if (
            not self.deep_orderbook.bids
            or not self.deep_orderbook.asks
        ):
            return False
        if not self.deep_book_is_coherent_with_fast():
            return False
        return age <= self.deep_book_stale_after_seconds

    def deep_book_health(
        self,
        now: float | None = None,
    ) -> dict:
        return {
            "fresh": self.deep_book_is_fresh(now),
            "synced": self.deep_book_synced,
            "ageSeconds": self.deep_book_age_seconds(now),
            "staleAfterSeconds": (
                self.deep_book_stale_after_seconds
            ),
            "bidLevels": len(self.depth_orderbook().bids),
            "askLevels": len(self.depth_orderbook().asks),
            "source": (
                "deep_l1000"
                if self.deep_book_synced is not None
                else "legacy_fast_fallback"
            ),
            "fastSeq": self.fast_book_seq,
            "deepSeq": self.deep_book_seq,
            "fastExchangeTsMs": (
                self.fast_book_exchange_ts_ms or None
            ),
            "deepExchangeTsMs": (
                self.deep_book_exchange_ts_ms or None
            ),
            "skewSeconds": self.deep_book_skew_seconds(),
            "maxSkewSeconds": self.deep_book_max_skew_seconds,
            "coherentWithFast": (
                self.deep_book_is_coherent_with_fast()
            ),
        }

    def confirmed_candle_age_seconds(
        self,
        now: float | None = None,
    ) -> float | None:
        confirmed = [c for c in self.candles if c.confirmed]
        if not confirmed:
            return None
        latest = max(confirmed, key=lambda candle: candle.start_ms)
        resolved_now = self.clock.time() if now is None else now
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
        resolved_now_ms = int(self.clock.time() * 1000) if now_ms is None else now_ms

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
            int(self.clock.time() * 1000)
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
        depth_book = self.depth_orderbook()
        rows = (
            depth_book.bids
            if wall_side == "bid"
            else depth_book.asks
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
            int(self.clock.time() * 1000)
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
            "fastEventRequests": self.fast_event_requests,
            "fastEventEvaluations": self.fast_event_evaluations,
            "fastEventCoalesced": self.fast_event_coalesced,
            "lastFastEventReason": self.last_fast_event_reason,
            "lastFastEventAtMs": (
                self.last_fast_event_at_ms or None
            ),
            "marketIngest": {
                "fastQueueDepth": self.fast_market_queue_depth,
                "fastQueueLagMs": self.fast_market_queue_lag_ms,
                "fastQueueMaxLagMs": (
                    self.fast_market_queue_max_lag_ms
                ),
                "deepQueueDepth": self.deep_market_queue_depth,
                "deepQueueLagMs": self.deep_market_queue_lag_ms,
                "deepQueueMaxLagMs": (
                    self.deep_market_queue_max_lag_ms
                ),
            },
        }

    def market_context_public(self) -> dict:
        if self.market_context is not None:
            return self.market_context.public()
        return {
            "schemaVersion": 1,
            "symbol": self.symbol,
            "instrument": (
                self.instrument.public()
                if self.instrument is not None
                else None
            ),
            "feeSchedule": (
                self.fee_schedule.public()
                if self.fee_schedule is not None
                else None
            ),
            "funding": self.funding_public(),
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
        now_ms = (self.clock_reading or {}).get("evaluationMs") or int(self.clock.time() * 1000)
        return {
            "clock": self.clock_reading,
            "symbol": self.symbol,
            "lastPrice": self.last_price,
            "trend": self.trend.value,
            "marketContext": self.market_context_public(),
            "analysisRuntime": self.analysis_runtime_public(),
            "scenarioRouting": self.scenario_view,
            "candles": [x.public() for x in self.candles[-240:]],
            "chartSeries": self.chart_series(now_ms),
            "orderbook": self.orderbook.public(50),
            "fastOrderbook": self.orderbook.public(50),
            "deepOrderbook": self.depth_orderbook().public(50),
            "densityContext": self.density_context(now_ms),
            "bookHealth": self.book_health(),
            "fastBookHealth": self.book_health(),
            "deepBookHealth": self.deep_book_health(),
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
        *,
        trade_after_sequence: int | None = None,
    ) -> dict:
        now_ms = (self.clock_reading or {}).get("evaluationMs") or int(self.clock.time() * 1000)
        all_trades = list(self.trades)
        if trade_after_sequence is None:
            selected_trades = (
                all_trades[-max(0, recent_trade_limit):]
                if recent_trade_limit > 0
                else []
            )
            trade_encoding = "rolling_v1"
            trade_delta_gap = False
        else:
            selected_trades = [
                trade
                for trade in all_trades
                if trade.sequence > trade_after_sequence
            ]
            trade_encoding = "delta_v1"
            first_sequence = (
                selected_trades[0].sequence
                if selected_trades
                else None
            )
            trade_delta_gap = bool(
                first_sequence is not None
                and first_sequence
                > trade_after_sequence + 1
            )
        return {
            "lastPrice": self.last_price,
            "clock": self.clock_reading,
            "trend": self.trend.value,
            "marketContext": self.market_context_public(),
            "analysisRuntime": self.analysis_runtime_public(),
            "scenarioRouting": self.scenario_view,
            "candle": self.candles[-1].public() if self.candles else None,
            "orderbook": self.orderbook.public(
                min(book_depth, 50)
            ),
            "fastOrderbook": self.orderbook.public(
                min(book_depth, 50)
            ),
            "deepOrderbook": self.depth_orderbook().public(
                book_depth
            ),
            "bookHealth": self.book_health(),
            "fastBookHealth": self.book_health(),
            "deepBookHealth": self.deep_book_health(),
            "candleHealth": self.candle_health(
                self.confirmed_candle_stale_after_seconds
            ),
            "tradeFlow": compute_trade_flow(list(self.trades), now_ms),
            "bookFlow": self.book_flow_snapshot(now_ms),
            "position": position,
            "funding": self.funding_public(),
            "tradeEncoding": trade_encoding,
            "tradeCursor": self.trade_sequence,
            "tradeDeltaFromSequence": (
                trade_after_sequence
                if trade_after_sequence is not None
                else None
            ),
            "tradeDeltaGap": trade_delta_gap,
            "recentTrades": [
                trade.public()
                for trade in selected_trades
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


class TradingEngine(ScenarioRuntime):
    def __init__(self, config: Settings, *, clock: RuntimeClock | None = None,
                 capture_inputs: bool = False, rest_client=None, recorder=None,
                 research_policy=None, configure_observability: bool = True) -> None:
        self.clock = clock if clock is not None else SystemRuntimeClock()
        self.config = config
        if configure_observability:
            configure_telemetry(config)
        self.rest = rest_client if rest_client is not None else BybitRestClient(config)
        self.risk = RiskEngine(config)
        self.broker = PaperBroker(config, clock=self.clock)
        self.router = ScenarioRouter(preparation_seconds=config.active_symbol_idle_timeout_seconds)
        self.recorder = recorder if recorder is not None else SessionRecorder(
            config.session_dir,
            clock=self.clock,
            queue_size=config.recorder_queue_size,
            critical_enqueue_timeout_seconds=(
                config.recorder_critical_enqueue_timeout_seconds
            ),
        )
        self.research_policy = research_policy if research_policy is not None else ResearchPolicyRuntime.from_settings(
            path=config.research_policy_file,
            mode=config.research_policy_mode,
        )
        default_strategies = create_default_strategies()
        self.strategies: dict[str, Strategy] = {
            strategy.key: strategy
            for strategy in default_strategies
        }
        self.broker.position_manager = lambda pos, gross: (
            self.strategies[pos.strategy].manage_progress(self.config,self.clock,pos,gross)
            if pos.strategy in self.strategies else False)
        configured_strategy_state = {
            "trend_structure": config.trend_structure_enabled,
            "weak_level_rejection": config.weak_level_rejection_enabled,
            "orderbook_density": config.density_enabled,
            "level_breakout": config.breakout_enabled,
            # Opt-in through the existing UI/API toggle. The run manifest records
            # the actual enabled strategy set; baseline config stays unchanged.
            "price_action_hypothesis": config.price_action_hypothesis_enabled,
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
            for x in default_strategies
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
            breakout_strategy.conditional_hold_enabled = config.e06_conditional_breakout_hold
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
        self._event_tasks: set[asyncio.Task] = set()
        self._worker_tasks: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self._stop = asyncio.Event()
        self._paper_timer_task: asyncio.Task | None = None
        self._run_started_at: float | None = None
        self._run_started_mono: float | None = None
        self._run_deadline_at: float | None = None
        self._last_run_summary: dict | None = None
        self._run_manifest: dict | None = None
        self.market_clock = MarketClock(
            max_rtt_ms=config.clock_max_rtt_ms,
            max_sync_age_seconds=config.clock_max_sync_age_seconds,
            max_uncertainty_ms=config.clock_max_uncertainty_ms,
            wall_jump_ms=config.clock_wall_jump_ms,
            drift_ppm=config.clock_drift_ppm,
        )
        self._scanner_error: str | None = None
        self._last_scan_ok_at: float | None = None
        self._last_scan_error_at: float | None = None
        self._pending_order_latency: dict[
            tuple[str, str],
            MarketMessage,
        ] = {}
        self._arbiter_latency_message: MarketMessage | None = None
        self._arbiter_trigger_symbol: str | None = None
        self._arbiter_trigger_setups: set[
            tuple[str, str]
        ] = set()
        # Experimental opt-in API until bootstrap/scheduler coverage is complete.
        # The journal header explicitly prohibits treating this as full parity.
        self._transport_worker_serial = 0
        self.input_scopes = None
        self._context_batch_serial = 0
        self._source_request_serial = 0
        self.input_journal = InputJournal(self.recorder.record, self.clock) if capture_inputs else None
        if self.input_journal is not None:
            self._record_input("manifest", None, {"phase": "capture", "manifest": build_run_manifest(
                self.config, self.strategy_enabled,
                code=code_provenance(Path(__file__).resolve().parents[1]),
                policy=self.research_policy.public())})
            self._record_input("policy_snapshot", None, {
                "mode": self.research_policy.mode.value,
                "manifest": self.research_policy.manifest,
                "contentHash": fingerprint(self.research_policy.manifest),
                "sourceFileSha256": self.research_policy.source_sha256})
            self.input_scopes = InputScopes(self.input_journal)
            self.clock = RecordingRuntimeClock(self.clock, self._record_clock_read)
            self.broker.clock = self.clock
            # Recorder metadata and journal envelopes retain the undecorated
            # clock; recording a clock read must never recursively record itself.

    def _record_clock_read(self, method, value) -> None:
        if self.input_journal is not None and not self.input_journal.closed:
            self._record_input("clock_read", None, {"method": method, "value": value,
                "scopeId": self.input_scopes.current.get()})

    def _record_scheduler(self, phase, symbol, task_id, reason="", delay=0.0, outcome=None):
        if task_id is not None:
            self._record_input("scheduler", symbol, {"phase": phase, "taskId": task_id,
                "reason": reason, "delay": delay, "outcome": outcome})


    def _record_input(self, kind: str, symbol: str | None, body: dict) -> None:
        if self.input_journal is not None:
            self.input_journal.append(kind, symbol, body)

    async def start(self) -> None:
        self._record_input("service", None, {"phase": "start"})
        self._stop.clear()
        self.recorder.start_background_writer()
        if self.config.exchange_clock_enabled:
            await self._sync_clock_once()
        try:
            await self._scan_once()
        except Exception as exc:
            self._record_scanner_error("startup_scan_error", exc)
        self._launch_service_tasks()

    def _launch_service_tasks(self):
        self._tasks = [
            asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._context_loop(), name="context"),
            asyncio.create_task(self._arbiter_loop(), name="trade-arbiter"),
        ]
        if self.config.exchange_clock_enabled:
            self._tasks.append(asyncio.create_task(self._clock_loop(), name="exchange-clock"))

    @input_scope("clock_sync", symbol_arg=False)
    async def _sync_clock_once(self) -> bool:
        try:
            with source_await(self, 'clock'):
                try:
                    sample = await self.rest.clock_sample()
                except Exception as exc:
                    sample = SourceFailure(type(exc).__name__)
            if isinstance(sample, SourceFailure):
                return self._apply_clock_error(sample.error_type)
            return self._apply_clock_sample(sample)
        except Exception as exc:
            # Keep the last bounded sample only until its configured expiry.
            return self._apply_clock_error(type(exc).__name__)

    def _apply_clock_sample(self, sample):
        """Shared application of a recorded or live REST clock result."""
        self._record_input("clock_sample", None, sample)
        accepted = self.market_clock.synchronize(**sample)
        self._emit("clock_sync", None, {"sample": sample, "accepted": accepted,
                   "rejectionReason": self.market_clock.last_sync_rejection,
                   "upperPaddingMs": self.market_clock.last_sync_upper_padding_ms,
                   "clock": self._clock_state()})
        return accepted

    def _apply_clock_error(self, error_type):
        self._record_input("clock_error", None, {"errorType": error_type})
        self._emit("clock_sync_error", None, {"errorType": error_type})
        return False

    async def _input_sleep(self, source: str, delay: float) -> None:
        if self.input_journal is None:
            await self._periodic_sleep(delay)
            return
        identity = self.input_journal.sequence + 1
        body = {"source": source, "id": identity, "delay": delay}
        self._record_input("dispatch", None, {**body, "phase": "wait"})
        try:
            await self._periodic_sleep(delay)
        except asyncio.CancelledError:
            self._record_input("dispatch", None, {**body, "phase": "cancelled"})
            raise
        self._record_input("dispatch", None, {**body, "phase": "wake"})

    async def _periodic_sleep(self, delay):
        """Live wait adapter; replay supplies a controlled suspension."""
        await asyncio.sleep(delay)

    @input_scope("clock_loop")
    async def _clock_loop(self) -> None:
        interval = max(1.0, self.config.clock_sync_interval_seconds)
        retry = min(2.0, interval)
        delay = interval if self._clock_state()["valid"] else retry
        while not self._stop.is_set():
            await self._input_sleep("clock", delay)
            if self._stop.is_set():
                break
            accepted = await self._sync_clock_once()
            delay = interval if accepted else retry

    def _clock_state(self, session: ActiveSymbolSession | None = None) -> dict | None:
        if not self.config.exchange_clock_enabled:
            return None
        reading = self.market_clock.read(
            mono=self.clock.perf_counter_ns() / 1e9, wall_ms=self.clock.time() * 1000,
            latest_processed_event_ms=session.latest_processed_event_ms if session else None,
        )
        state = {**asdict(reading), "evaluationMs": reading.evaluation_ms,
                 "contract": "exchange-bounds-v1"}
        if session is not None:
            session.receipt_clock_required = True
            state["receiptAgesSeconds"] = {
                key: None if stamp is None else receipt_age_seconds(
                    now_mono=reading.monotonic_seconds, received_mono=stamp)
                for key, stamp in (("fastBook", session.fast_receipt_mono),
                                   ("deepBook", session.deep_receipt_mono),
                                   ("trade", session.trade_receipt_mono))
            }
            session.clock_reading = state
            if not reading.valid:
                session.decisions.clear()
        return state

    def _clock_entry_block(self, session: ActiveSymbolSession) -> str | None:
        state = self._clock_state(session)
        if state is None:
            return None
        reason = None
        if not state["valid"]:
            reason = "clock:" + str(state["reason"])
        elif not session.book_is_fresh() or not session.deep_book_is_fresh():
            reason = "clock:stale_book_receipt"
        else:
            age = state["receiptAgesSeconds"]["trade"]
            if age is None or age > self.config.trade_receipt_stale_seconds:
                reason = "clock:stale_trade_receipt"
            else:
                for name, stamp, limit in (
                    ("fast_book", session.fast_book_exchange_ts_ms, session.book_stale_after_seconds),
                    ("deep_book", session.deep_book_exchange_ts_ms, session.deep_book_stale_after_seconds),
                    ("trade", session.trade_exchange_ts_ms, self.config.trade_receipt_stale_seconds),
                ):
                    if stamp <= 0 or state["exchange_lower_ms"] - stamp > limit * 1000:
                        reason = "clock:stale_" + name + "_event"
                        break
        state["entryBlockReason"] = reason
        if reason != session.clock_block_reason:
            session.clock_block_reason = reason
            self._emit("clock_admission_changed", session.symbol, {"reason": reason, "clock": state})
        return reason

    async def close(self) -> None:
        self._record_input("service", None, {"phase": "close"})
        if self.running:
            self._stop_trading("shutdown")
        else:
            self._cancel_all_pending("shutdown")
            self._close_all_positions("shutdown")
        self._cancel_run_timer()
        self._stop.set()
        try:
            await self._shutdown_service_tasks()
        finally:
            try:
                await asyncio.wait_for(self.rest.close(), timeout=5)
            finally:
                # Even failed task/REST teardown drains accepted data. Capture
                # marks the failure incomplete; footer alone is not success.
                if self.input_journal is not None:
                    self.input_journal.close()
                self.recorder.close()

    async def _shutdown_service_tasks(self):
        for task, stop_event in self._worker_tasks.values():
            stop_event.set()
            task.cancel()
        for task in self._tasks:
            task.cancel()
        for task in list(self._event_tasks):
            task.cancel()
        closing = {x[0] for x in self._worker_tasks.values()} | set(self._tasks) | set(self._event_tasks)
        closing.discard(asyncio.current_task())
        if closing:
            done, pending = await asyncio.wait(closing, timeout=5)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Retrieve failures; no lost task exceptions.
            if pending:
                raise TimeoutError("service tasks did not acknowledge shutdown")
        self._event_tasks.clear()

    @input_scope("start_request", symbol_arg=False)
    def set_running(self, value: bool) -> None:
        self._record_input("control", None, {"name": "set_running", "value": value})
        if value:
            if self.running:
                return
            blocked = self.start_block_reason()
            if blocked:
                raise RuntimeError(
                    f"cannot start paper run: {blocked}"
                )
            manifest = self._build_trading_manifest()
            now = self.clock.time()
            self._record_input("manifest", None, {"phase": "run", "manifest": manifest})
            self._run_manifest = manifest
            self.running = True
            self._run_started_at = now
            self._run_started_mono = self.clock.perf_counter_ns() / 1e9
            self._run_deadline_at = now + self.config.paper_run_duration_seconds
            self._last_run_summary = None
            self._cancel_run_timer()
            self._launch_run_timer()
            self._emit(
                "bot_started",
                None,
                {
                    "runLabel": self.config.run_label,
                    "startedAt": self._run_started_at,
                    "deadlineAt": self._run_deadline_at,
                    "durationSeconds": self.config.paper_run_duration_seconds,
                    "config": self._run_config_snapshot(),
                    "manifest": manifest,
                    "clock": self._clock_state(),
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

    def _build_trading_manifest(self):
        return build_run_manifest(self.config, self.strategy_enabled,
            code=code_provenance(Path(__file__).resolve().parents[1]), policy=self.research_policy.public())

    def _launch_run_timer(self):
        self._paper_timer_task = asyncio.create_task(self._paper_run_timer(), name="paper-run-timer")

    @input_scope("paper_timer")
    async def _paper_run_timer(self) -> None:
        try:
            await self._input_sleep("paper_timer", self.config.paper_run_duration_seconds)
        except asyncio.CancelledError:
            raise
        if self.running:
            self._stop_trading("duration_elapsed", cancel_timer=False)

    @input_scope("stop", symbol_arg=False)
    def _stop_trading(self, reason: str, *, cancel_timer: bool = True) -> None:
        self._record_input("control", None, {"name": "stop", "reason": reason})
        if (
            not self.running
            and not self.broker.positions
            and not self.broker.pending_entries
        ):
            return
        stopped_at = self.clock.time()
        self.running = False
        if cancel_timer:
            self._cancel_run_timer()
        self._cancel_all_pending(reason)
        self._close_all_positions(reason)
        started_at = self._run_started_at
        summary = {
            "manifestId": self._run_manifest["manifestId"] if self._run_manifest else None,
            "manifestSha256": self._run_manifest["manifestSha256"] if self._run_manifest else None,
            "runLabel": self.config.run_label,
            "reason": reason,
            "startedAt": started_at,
            "stoppedAt": stopped_at,
            "elapsedSeconds": (
                max(0.0, self.clock.perf_counter_ns() / 1e9 - self._run_started_mono)
                if self.config.exchange_clock_enabled and self._run_started_mono is not None
                else max(0.0, stopped_at - started_at) if started_at else 0.0
            ),
            "elapsedClock": "monotonic" if self.config.exchange_clock_enabled else "local_wall",
            "configuredDurationSeconds": self.config.paper_run_duration_seconds,
            "balance": self.broker.balance,
            "realizedPnl": self.broker.total_pnl,
            "closedTrades": self.broker.total_closed_trades,
            "latencyMetrics": latency_metrics_snapshot(),
            "recorderHealth": self.recorder.health(),
        }
        self._last_run_summary = summary
        if self._run_manifest is not None:
            self._record_input("run_end", None, {"manifestId": self._run_manifest["manifestId"],
                "manifestSha256": self._run_manifest["manifestSha256"], "reason": reason})
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
            "fastOrderbookDepth": self.config.fast_orderbook_depth,
            "deepOrderbookDepth": self.config.deep_orderbook_depth,
            "eventDrivenEvaluation": (
                self.config.event_driven_evaluation_enabled
            ),
            "eventEvaluationMinIntervalSeconds": (
                self.config.event_evaluation_min_interval_seconds
            ),
            "marketQueueSize": self.config.market_queue_size,
            "marketQueuePutTimeoutSeconds": (
                self.config.market_queue_put_timeout_seconds
            ),
            "marketQueueMaxLagSeconds": (
                self.config.market_queue_max_lag_seconds
            ),
            "prometheusEnabled": self.config.prometheus_enabled,
            "otelEnabled": self.config.otel_enabled,
            "otelServiceName": self.config.otel_service_name,
            "otelExporterOtlpEndpoint": (
                self.config.otel_exporter_otlp_endpoint
            ),
            "otelTraceSampleRatio": (
                self.config.otel_trace_sample_ratio
            ),
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

    @input_scope("toggle_strategy", symbol_arg=False)
    def toggle_strategy(self, key: str, enabled: bool) -> None:
        self._record_input("control", None, {"name": "toggle_strategy", "key": key, "enabled": enabled})
        if key not in self.strategy_enabled:
            raise KeyError(key)
        self.strategy_enabled[key] = enabled
        if key == "orderbook_density" and not enabled:
            for session in self.sessions.values():
                session.liquidity_evidence = None
        self._emit("strategy_toggle", None, {
            "strategy": key, "enabled": enabled,
            "manifestId": self._run_manifest["manifestId"] if self.running and self._run_manifest else None,
        })

    @input_scope("scanner_loop")
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
                await self._input_sleep("scanner", max(0.1, delay))
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
        failure = describe_source_error(exc)
        self._record_input("source_error", None, {"source": "scanner", "errorType": failure.error_type})
        self._scanner_error = f"{failure.error_type}: {failure.message}"
        self._last_scan_error_at = self.clock.time()
        self._emit(event, None, {"error": self._scanner_error})

    @input_scope("market_health")
    def market_health(self) -> dict:
        now = self.clock.time()
        clock = self._clock_state()
        market_now = clock["evaluationMs"] / 1000 if clock and clock["valid"] else now
        clock_blocks = {
            session.symbol: self._clock_entry_block(session)
            for session in self.sessions.values()
        }
        live_sessions = [
            session
            for session in self.sessions.values()
            if session.last_market_at > 0
            and now - session.last_market_at <= self.config.market_stale_seconds
            and session.book_is_fresh(now)
            and session.deep_book_is_fresh(now)
            and session.confirmed_candle_is_fresh(
                self.config.confirmed_candle_stale_seconds,
                market_now,
            )
            and clock_blocks[session.symbol] is None
        ]
        ready = bool(self.candidates) and bool(live_sessions)
        if clock is not None and not clock["valid"]:
            ready = False
            reason = "exchange clock is not ready: " + str(clock["reason"])
        elif self._scanner_error:
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
                    market_now,
                )
            ]
            stale_deep_books = [
                session.symbol
                for session in self.sessions.values()
                if (
                    session.book_is_fresh(now)
                    and not session.deep_book_is_fresh(now)
                )
            ]
            reason = (
                "confirmed 1m candle history is stale: "
                + ", ".join(stale_candles[:6])
                if stale_candles
                else (
                    "waiting for synchronized deep L1000 book: "
                    + ", ".join(stale_deep_books[:6])
                    if stale_deep_books
                    else (
                        "entry data checks: " + "; ".join(
                            f"{symbol}: {block}" for symbol, block in clock_blocks.items() if block
                        ) if any(clock_blocks.values())
                        else "waiting for fresh synchronized websocket market data"
                    )
                )
            )
        else:
            reason = None
        return {
            "ready": ready,
            "clock": clock,
            "clockBlocks": clock_blocks,
            "reason": reason,
            "scannerError": self._scanner_error,
            "lastScanOkAt": self._last_scan_ok_at,
            "lastScanErrorAt": self._last_scan_error_at,
            "candidateCount": len(self.candidates),
            "activeSymbolCount": len(self.sessions),
            "liveSymbolCount": len(live_sessions),
            "fastBookReadyCount": sum(
                1
                for session in self.sessions.values()
                if session.book_is_fresh(now)
            ),
            "deepBookReadyCount": sum(
                1
                for session in self.sessions.values()
                if session.deep_book_is_fresh(now)
            ),
        }

    def start_block_reason(self) -> str | None:
        state = self._clock_state()
        if state is not None and not state["valid"]:
            return "exchange clock: " + str(state["reason"])
        health = self.market_health()
        return None if health["ready"] else str(
            health["reason"] or "market data is not ready"
        )

    @input_scope("scan", symbol_arg=False)
    async def _scan_once(self) -> None:
        with source_await(self, 'scanner'):
            candidates = await self.rest.active_candidates()
        await self._apply_scanner_result(candidates)

    async def _apply_scanner_result(self, candidates) -> None:
        """Shared ranking, promotion and cleanup after a live or recorded scan."""
        if self.input_journal is not None:
            self._record_input("scanner_result", None, {"candidates": [asdict(c) for c in candidates]})
        if not candidates:
            raise RuntimeError(
                "scanner returned zero eligible candidates"
            )
        self.candidates = candidates
        self._scanner_error = None
        self._last_scan_ok_at = self.clock.time()
        now = self.clock.time()
        candidate_map = {item.symbol: item for item in self.candidates}

        for symbol, session in self.sessions.items():
            candidate = candidate_map.get(symbol)
            if candidate is not None:
                session.mark_price = candidate.mark_price
                session.funding_rate = candidate.funding_rate
                session.next_funding_time_ms = (
                    candidate.next_funding_time_ms
                )
                if (
                    (candidate.activity_rank or 999)
                    <= self.config.active_keep_rank
                ):
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
            self._record_input("source_error", symbol, {"source": "bootstrap", "errorType": describe_source_error(exc).error_type})
            self._emit(
                "symbol_bootstrap_error",
                symbol,
                {"error": str(exc)},
            )
            return
        self.sessions[symbol].last_ranked_at = now
        self._launch_symbol_worker(symbol)

    def _launch_symbol_worker(self, symbol):
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

    def _fast_book_event_reason(
        self,
        session: ActiveSymbolSession,
        previous: OrderBook,
        current: OrderBook,
        *,
        ofi_usd: float = 0.0,
    ) -> str | None:
        if not self.config.event_driven_evaluation_enabled:
            return None
        engaged = (
            self._session_engaged(session)
            or session.symbol in self.broker.positions
            or session.symbol in self.broker.pending_entries
        )
        if not engaged:
            return None
        if not current.bids or not current.asks:
            return None
        if not previous.bids or not previous.asks:
            return "fast_book_ready"

        if (
            previous.best_bid != current.best_bid
            or previous.best_ask != current.best_ask
        ):
            return "best_quote"

        previous_mid = previous.mid
        current_mid = current.mid
        if previous_mid and current_mid:
            mid_move_bps = (
                abs(current_mid - previous_mid)
                / previous_mid
                * 10_000
            )
            if (
                mid_move_bps
                >= self.config.fast_event_min_mid_move_bps
            ):
                return "mid_move"

        spread_change_bps = (
            abs(current.spread_pct - previous.spread_pct)
            * 10_000
        )
        if (
            spread_change_bps
            >= self.config.fast_event_min_spread_change_bps
        ):
            return "spread_change"

        top_depth = sum(
            price * qty
            for price, qty in (
                current.bids[:5] + current.asks[:5]
            )
        )
        if top_depth > 0:
            ofi_fraction = abs(ofi_usd) / top_depth
            if (
                ofi_fraction
                >= self.config.fast_event_min_ofi_fraction
            ):
                return "top_level_ofi"
        return None

    @staticmethod
    def _tradeable_event_fingerprint(
        session: ActiveSymbolSession,
    ) -> tuple:
        return tuple(sorted(
            (
                key,
                decision.action.value,
                str(decision.setup_id or ""),
                str((decision.details or {}).get("state") or ""),
            )
            for key, decision in session.decisions.items()
            if decision.tradeable
        ))

    def _track_event_task(
        self,
        task: asyncio.Task,
        *, capture_id=None, symbol=None,
    ) -> None:
        self._event_tasks.add(task)
        def completed(done):
            try:
                if capture_id is not None:
                    outcome = "cancelled" if done.cancelled() else "raised" if done.exception() is not None else "returned"
                    self._record_scheduler("finished", symbol, capture_id, outcome=outcome)
            finally:
                self._complete_event_evaluation(symbol, done)
                self._event_tasks.discard(done)
        task.add_done_callback(completed)

    def _complete_event_evaluation(self, symbol, owner):
        session = self.sessions.get(symbol)
        if session is not None and session.event_eval_owner is owner:
            session.event_eval_pending = False
            session.event_eval_capture_id = None
            session.pending_latency_message = None
            session.event_eval_owner = None

    def _schedule_event_evaluation(
        self,
        session: ActiveSymbolSession,
        reason: str,
        *,
        observed_at_ms: int | None = None,
        market_message: MarketMessage | None = None,
    ) -> None:
        if not self.config.event_driven_evaluation_enabled:
            return
        if not (
            self._session_engaged(session)
            or session.symbol in self.broker.positions
            or session.symbol in self.broker.pending_entries
        ):
            return

        session.fast_event_requests += 1
        session.last_fast_event_reason = reason
        if market_message is not None:
            session.pending_latency_message = market_message
        session.last_fast_event_at_ms = (
            int(self.clock.time() * 1000)
            if observed_at_ms is None
            else int(observed_at_ms)
        )
        if session.event_eval_pending:
            session.fast_event_coalesced += 1
            self._record_scheduler("coalesced", session.symbol, session.event_eval_capture_id, reason)
            return

        session.event_eval_pending = True
        capture_id = self.input_journal.sequence + 1 if self.input_journal is not None else None
        session.event_eval_capture_id = capture_id
        self._record_scheduler("scheduled", session.symbol, capture_id, reason)
        self._launch_event_evaluation(session.symbol, reason, capture_id)

    def _launch_event_evaluation(self, symbol, reason, capture_id):
        """Live task adapter; offline replay supplies a deterministic scheduler."""
        task = asyncio.create_task(
            self._run_event_evaluation(
                symbol,
                reason,
                capture_id=capture_id,
            ),
            name=f"fast-eval-{symbol}",
        )
        self.sessions[symbol].event_eval_owner = task
        self._track_event_task(task, capture_id=capture_id, symbol=symbol)

    async def _event_evaluation_sleep(self, delay):
        await asyncio.sleep(delay)

    @input_scope("event_evaluation", symbol_arg=True)
    async def _run_event_evaluation(
        self,
        symbol: str,
        reason: str,
        *, capture_id=None,
    ) -> None:
        self._record_scheduler("started", symbol, capture_id, reason)
        session = self.sessions.get(symbol)
        if session is None:
            return
        try:
            min_interval = max(
                0.0,
                float(
                    self.config
                    .event_evaluation_min_interval_seconds
                ),
            )
            elapsed = (
                self.clock.monotonic() - session.last_event_eval_at
                if session.last_event_eval_at > 0
                else min_interval
            )
            delay = max(0.0, min_interval - elapsed)
            if delay > 0:
                self._record_scheduler("sleep", symbol, capture_id, reason, delay)
                await self._event_evaluation_sleep(delay)
                self._record_scheduler("resumed", symbol, capture_id, reason, delay)

            session = self.sessions.get(symbol)
            if session is None:
                return
            before = self._tradeable_event_fingerprint(
                session
            )
            now = self.clock.monotonic()
            session.last_eval = now
            session.last_event_eval_at = now
            session.fast_event_evaluations += 1
            session.last_fast_event_reason = reason
            latency_message = session.pending_latency_message
            evaluation_started_ns = self.clock.perf_counter_ns()

            with span(
                "strategy.event_evaluate",
                **{
                    "market.event_id": (
                        latency_message.event_id
                        if latency_message is not None
                        else None
                    ),
                    "market.symbol": symbol,
                    "strategy.trigger_reason": reason,
                },
            ):
                await self._evaluate(session)

            if latency_message is not None:
                if (
                    latency_message.strategy_eval_started_mono_ns
                    <= 0
                ):
                    latency_message.strategy_eval_started_mono_ns = (
                        evaluation_started_ns
                    )
                    observe_latency(
                        "parse_to_strategy",
                        max(
                            0.0,
                            (
                                evaluation_started_ns
                                - latency_message.parsed_mono_ns
                            )
                            / 1_000_000_000,
                        )
                        if latency_message.parsed_mono_ns > 0
                        else None,
                        stream=stream_name(latency_message.topic),
                        status="fallback",
                    )
                latency_message.strategy_eval_finished_mono_ns = (
                    self.clock.perf_counter_ns()
                )
                observe_latency(
                    "strategy_evaluation",
                    max(
                        0.0,
                        (
                            latency_message.strategy_eval_finished_mono_ns
                            - latency_message.strategy_eval_started_mono_ns
                        )
                        / 1_000_000_000,
                    ),
                    stream=stream_name(latency_message.topic),
                )
            after = self._tradeable_event_fingerprint(
                session
            )

            # FIRE should not wait for the periodic 250ms arbiter tick. Keep
            # the global selection/risk semantics intact, but invoke them as
            # soon as this market event creates a new tradeable setup.
            if (
                self.running
                and after
                and after != before
            ):
                before_setups = {
                    (str(row[0]), str(row[2]))
                    for row in before
                }
                after_setups = {
                    (str(row[0]), str(row[2]))
                    for row in after
                }
                new_fire_setups = (
                    after_setups - before_setups
                )
                if latency_message is not None and new_fire_setups:
                    latency_message.fire_mono_ns = self.clock.perf_counter_ns()
                    strategy_name = sorted(
                        new_fire_setups
                    )[0][0]
                    observe_latency(
                        "strategy_to_fire",
                        max(
                            0.0,
                            (
                                latency_message.fire_mono_ns
                                - latency_message.strategy_eval_started_mono_ns
                            )
                            / 1_000_000_000,
                        ),
                        stream=stream_name(latency_message.topic),
                        strategy=strategy_name,
                    )
                    exchange_receive = exchange_receive_seconds(
                        latency_message
                    )
                    receipt_to_fire = max(
                        0.0,
                        (
                            latency_message.fire_mono_ns
                            - latency_message.receipt_mono_ns
                        )
                        / 1_000_000_000,
                    )
                    if (
                        exchange_receive is not None
                        and exchange_receive >= 0
                    ):
                        observe_latency(
                            "exchange_to_fire",
                            exchange_receive + receipt_to_fire,
                            stream=stream_name(latency_message.topic),
                            strategy=strategy_name,
                        )
                    trace_snapshot = latency_snapshot(
                        latency_message
                    )
                    for decision in session.decisions.values():
                        setup_key = (
                            decision.strategy,
                            str(decision.setup_id or ""),
                        )
                        if setup_key in new_fire_setups:
                            decision.details[
                                "latencyTrace"
                            ] = trace_snapshot
                self._arbiter_latency_message = (
                    latency_message
                    if new_fire_setups
                    else None
                )
                self._arbiter_trigger_symbol = symbol
                self._arbiter_trigger_setups = (
                    new_fire_setups
                )
                try:
                    self._arbitrate_once()
                finally:
                    self._arbiter_latency_message = None
                    self._arbiter_trigger_symbol = None
                    self._arbiter_trigger_setups = set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(
                "fast_path_error",
                symbol,
                {
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            current = self.sessions.get(symbol)
            if current is not None:
                current.event_eval_pending = False
                current.event_eval_capture_id = None
                current.pending_latency_message = None

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
            return self.config.deep_orderbook_depth
        return min(self.config.deep_orderbook_depth, 50)

    def _deactivate_symbol(self, symbol: str, reason: str) -> None:
        self._record_input("symbol_lifecycle", symbol, {"action": "deactivate", "reason": reason})
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
        with source_await(self, 'bootstrap', symbol):
            result = await self._fetch_bootstrap_result(symbol)
        self._apply_bootstrap_result(symbol, result)

    async def _fetch_bootstrap_result(self, symbol: str):
        (
            instrument,
            fee_schedule,
            candles,
            context_5m,
            context_15m,
            context_1h,
        ) = await asyncio.gather(
            self.rest.instrument_info(symbol),
            self.rest.fee_schedule(symbol),
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
        return instrument, fee_schedule, candles, context_5m, context_15m, context_1h

    @input_scope("bootstrap_apply", symbol_arg=True)
    def _apply_bootstrap_result(self, symbol: str, result) -> None:
        """Apply the complete bootstrap without consulting REST."""
        instrument, fee_schedule, candles, context_5m, context_15m, context_1h = result
        if self.input_journal is not None:
            self._record_input("bootstrap", symbol, {
                "instrument": asdict(instrument) if instrument is not None else None,
                "fees": asdict(fee_schedule) if fee_schedule is not None else None,
                "candles": [asdict(c) for c in candles],
                "context5m": [asdict(c) for c in context_5m],
                "context15m": [asdict(c) for c in context_15m],
                "context1h": [asdict(c) for c in context_1h]})
        now = self.clock.time()
        scanner_candidate = next(
            (
                item
                for item in self.candidates
                if item.symbol == symbol
            ),
            None,
        )
        session = ActiveSymbolSession(
            clock=self.clock,
            symbol=symbol,
            candles=candles,
            instrument=instrument,
            fee_schedule=fee_schedule,
            mark_price=(
                scanner_candidate.mark_price
                if scanner_candidate is not None
                else 0.0
            ),
            funding_rate=(
                scanner_candidate.funding_rate
                if scanner_candidate is not None
                else None
            ),
            next_funding_time_ms=(
                scanner_candidate.next_funding_time_ms
                if scanner_candidate is not None
                else None
            ),
            context_5m=[x for x in context_5m if x.confirmed],
            context_15m=[x for x in context_15m if x.confirmed],
            context_1h=[x for x in context_1h if x.confirmed],
            book_stale_after_seconds=self.config.book_stale_seconds,
            deep_book_stale_after_seconds=(
                self.config.deep_book_stale_seconds
            ),
            book_synced=False,
            deep_book_synced=False,
            deep_book_max_skew_seconds=(
                self.config.deep_book_max_skew_seconds
            ),
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
        self._record_input("symbol_lifecycle", symbol, {"action": "activate", "reason": "bootstrap_completed"})
        self._emit(
            "symbol_activated",
            symbol,
            {
                "market": session.market_snapshot(),
                "reason": "promoted from liquid activity ranking",
            },
        )

    @input_scope("context_loop")
    async def _context_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._input_sleep("context", 60)
                items = list(self.sessions.items())
                if not items:
                    continue
                results = await self._fetch_context_results(items)
                for (symbol, session), result in zip(
                    items,
                    results,
                    strict=True,
                ):
                    if isinstance(result, (Exception, SourceFailure)):
                        error_type = result.error_type if isinstance(result, SourceFailure) else type(result).__name__
                        self._record_input("source_error", symbol, {"source": "context", "errorType": error_type})
                        continue
                    self._apply_context_result(session, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_input("source_error", None, {"source": "context", "errorType": type(exc).__name__})
                self._emit("context_error", None, {"error": str(exc)})

    def _context_needs_1m(self, session):
        return not session.confirmed_candle_is_fresh(self.config.confirmed_candle_stale_seconds)

    async def _fetch_context_results(self, items):
        """Live REST adapter; the loop applies results in the original item order."""
        identity = self._begin_context_batch(items)
        async def refresh(
            symbol: str,
            session: ActiveSymbolSession,
        ):
            self._record_input("context_await", symbol, {"id": identity, "phase": "request"})
            stale_1m = self._context_needs_1m(session)
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

        try:
            results = await asyncio.gather(
                *(refresh(symbol, session) for symbol, session in items),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            self._record_input("context_await", None, {"id": identity, "phase": "cancelled"})
            raise
        self._record_input("context_await", None, {"id": identity, "phase": "ready"})
        return results

    def _begin_context_batch(self, items):
        self._context_batch_serial += 1
        identity = self._context_batch_serial
        self._record_input("context_await", None, {"id": identity, "phase": "wait",
            "symbols": [symbol for symbol, _ in items]})
        return identity

    @input_scope("rest_context_apply", symbol_arg=True)
    def _apply_context_result(self, session: ActiveSymbolSession, result) -> None:
        """Apply one REST result; shared boundary for live and future replay."""
        symbol = session.symbol
        if self.input_journal is not None:
            self._record_input("rest_context", symbol, {
                "candles": None if result[0] is None else [asdict(c) for c in result[0]],
                "context5m": [asdict(c) for c in result[1]],
                "context15m": [asdict(c) for c in result[2]],
                "context1h": [asdict(c) for c in result[3]]})
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

    async def _process_public_trade_message(
        self,
        session: ActiveSymbolSession,
        message: MarketMessage,
    ) -> list[TradeTick]:
        """Apply one public-trade batch without leaking later ticks backward.

        A resting maker order must see each exchange trade in causal order:
        the current tick may fill it, then strategy invalidation may react to
        that tick. Later trades from the same websocket batch must never be
        visible to an earlier fill/cancel decision.
        """
        rows = message.get("data") or []
        if not rows:
            return []

        ticks: list[TradeTick] = []
        wall_now = self.clock.time()
        for row in rows:
            session.trade_sequence += 1
            tick = TradeTick(
                ts_ms=int(row.get("T") or self.clock.time() * 1000),
                price=float(row["p"]),
                size=float(row["v"]),
                side=str(row.get("S") or ""),
                sequence=session.trade_sequence,
            )
            session.trades.append(tick)
            self._overlay_trade_on_forming_candle(
                session,
                tick,
            )
            session.last_price = tick.price
            session.last_trade_stream_at = wall_now
            session.trade_receipt_mono = (message.get("receipt_mono_ns", 0) or 0) / 1e9 or None
            session.latest_processed_event_ms = max(session.latest_processed_event_ms, tick.ts_ms)
            session.trade_exchange_ts_ms = max(session.trade_exchange_ts_ms, tick.ts_ms)
            prune_trades(
                session.trades,
                tick.ts_ms,
                self.config.trade_buffer_seconds,
            )
            ticks.append(tick)

            # Execution caused by this exact trade has priority over strategy
            # invalidation caused by the same trade. Otherwise a fill can be
            # retroactively cancelled after the market already traded through.
            self._mark_execution_from_market(
                session,
                trade_ts_ms=tick.ts_ms,
                trade_price=tick.price,
                trade_notional_usd=tick.notional,
                trade_side=tick.side,
            )

            # If the resting order survived this tick, only now may the
            # strategy use this tick to decide whether the order is still
            # valid before the next exchange trade is processed.
            if session.symbol in self.broker.pending_entries:
                session.last_eval = self.clock.monotonic()
                await self._evaluate(session)

        return ticks

    def _market_handler(self, symbol: str):
        """Create one worker's stateful handler, shared by live and offline adapters."""
        fast_depth = self.config.fast_orderbook_depth
        deep_depth = self.config.deep_orderbook_depth
        fast_book_state = OrderBookState(fast_depth)
        deep_book_state = OrderBookState(deep_depth)
        fast_topic = f"orderbook.{fast_depth}."
        deep_topic = f"orderbook.{deep_depth}."

        async def apply_message(
            message: MarketMessage | dict,
        ) -> None:
            if isinstance(message, dict):
                message = MarketMessage(
                    topic=message.get("topic"),
                    type=message.get("type"),
                    ts=message.get("ts"),
                    cts=message.get("cts"),
                    data=message.get("data"),
                    success=message.get("success"),
                    op=message.get("op"),
                )

            session = self.sessions.get(symbol)
            if session is None:
                return

            if self.input_journal is not None:
                self.input_journal.market_message(symbol, message)
            wall_now = self.clock.time()
            session.last_market_at = wall_now
            session.receipt_clock_required = self.config.exchange_clock_enabled
            topic = str(message.get("topic") or "")
            is_fast_book = topic.startswith(fast_topic)
            is_deep_book = topic.startswith(deep_topic)
            deep_only = (
                is_deep_book
                and not is_fast_book
            )

            queue_depth = int(
                getattr(message, "queue_depth", 0) or 0
            )
            queue_lag_ms = float(
                getattr(message, "queue_lag_ms", 0.0)
                or 0.0
            )
            if deep_only:
                session.deep_market_queue_depth = queue_depth
                session.deep_market_queue_lag_ms = queue_lag_ms
                session.deep_market_queue_max_lag_ms = max(
                    session.deep_market_queue_max_lag_ms,
                    queue_lag_ms,
                )
            else:
                session.fast_market_queue_depth = queue_depth
                session.fast_market_queue_lag_ms = queue_lag_ms
                session.fast_market_queue_max_lag_ms = max(
                    session.fast_market_queue_max_lag_ms,
                    queue_lag_ms,
                )

            if is_fast_book:
                previous_book = session.orderbook
                try:
                    session.orderbook = fast_book_state.apply(
                        message
                    )
                except OrderBookSequenceError:
                    session.last_book_at = 0.0
                    session.book_synced = False
                    session.orderbook = OrderBook()
                    raise
                session.book_synced = fast_book_state.synced
                session.fast_book_seq = fast_book_state.last_seq
                session.fast_book_exchange_ts_ms = int(
                    message.get("cts")
                    or message.get("ts")
                    or wall_now * 1000
                )
                session.last_book_at = wall_now
                session.fast_receipt_mono = message.receipt_mono_ns / 1e9 or None
                session.latest_processed_event_ms = max(
                    session.latest_processed_event_ms, int(message.cts or message.ts or 0))
                message.book_updated_mono_ns = self.clock.perf_counter_ns()
                observe_latency(
                    "processor_to_book",
                    max(
                        0.0,
                        (
                            message.book_updated_mono_ns
                            - message.processor_started_mono_ns
                        )
                        / 1_000_000_000,
                    )
                    if message.processor_started_mono_ns > 0
                    else None,
                    stream=stream_name(message.topic),
                )

                # If both configured depths are identical, the same stream is
                # authoritative for both roles.
                if fast_depth == deep_depth:
                    session.deep_orderbook = session.orderbook
                    session.deep_book_synced = (
                        fast_book_state.synced
                    )
                    session.deep_book_seq = fast_book_state.last_seq
                    session.deep_book_exchange_ts_ms = (
                        session.fast_book_exchange_ts_ms
                    )
                    session.last_deep_book_at = wall_now
                    session.deep_receipt_mono = session.fast_receipt_mono

                data = message.get("data") or {}
                ofi_usd = 0.0
                event_ms = int(
                    message.get("cts")
                    or message.get("ts")
                    or wall_now * 1000
                )
                if (
                    message.get("type") != "snapshot"
                    and int(data.get("u") or 0) != 1
                    and previous_book.bids
                    and previous_book.asks
                ):
                    ofi_usd = best_level_ofi_usd(
                        previous_book,
                        session.orderbook,
                    )
                    session.record_book_flow(
                        event_ms,
                        ofi_usd,
                    )

                # Stop/target checks consume the fast executable quote while
                # market-exit VWAP still uses the deep book.
                self._mark_position_from_book(session)
                reason = self._fast_book_event_reason(
                    session,
                    previous_book,
                    session.orderbook,
                    ofi_usd=ofi_usd,
                )
                if reason is not None:
                    self._schedule_event_evaluation(
                        session,
                        reason,
                        observed_at_ms=event_ms,
                        market_message=message,
                    )

            if deep_only:
                try:
                    session.deep_orderbook = (
                        deep_book_state.apply(message)
                    )
                except OrderBookSequenceError:
                    session.last_deep_book_at = 0.0
                    session.deep_book_synced = False
                    session.deep_orderbook = OrderBook()
                    raise
                session.deep_book_synced = (
                    deep_book_state.synced
                )
                session.deep_book_seq = deep_book_state.last_seq
                session.deep_book_exchange_ts_ms = int(
                    message.get("cts")
                    or message.get("ts")
                    or wall_now * 1000
                )
                session.last_deep_book_at = wall_now
                session.deep_receipt_mono = message.receipt_mono_ns / 1e9 or None
                session.latest_processed_event_ms = max(
                    session.latest_processed_event_ms, int(message.cts or message.ts or 0))
                message.book_updated_mono_ns = self.clock.perf_counter_ns()
                observe_latency(
                    "processor_to_book",
                    max(
                        0.0,
                        (
                            message.book_updated_mono_ns
                            - message.processor_started_mono_ns
                        )
                        / 1_000_000_000,
                    )
                    if message.processor_started_mono_ns > 0
                    else None,
                    stream=stream_name(message.topic),
                )

            if topic.startswith("kline."):
                self._apply_kline(session, message)
                session.last_kline_at = wall_now
            elif topic.startswith("publicTrade."):
                ticks = await self._process_public_trade_message(
                    session,
                    message,
                )
                if ticks:
                    session.last_price = ticks[-1].price
                    self._schedule_event_evaluation(
                        session,
                        "public_trade",
                        observed_at_ms=ticks[-1].ts_ms,
                        market_message=message,
                    )

            # Periodic evaluation remains a fallback, but deep-book-only
            # context updates never drive the latency-sensitive strategy loop.
            if not deep_only:
                now = self.clock.monotonic()
                evaluation_interval = (
                    self._evaluation_interval_seconds(
                        session
                    )
                )
                if (
                    not session.event_eval_pending
                    and now - session.last_eval
                    >= evaluation_interval
                ):
                    session.last_eval = now
                    await self._evaluate(session)

                position = self.broker.positions.get(symbol)
                self._clock_state(session)

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
                            (
                                position.public()
                                if position
                                else None
                            ),
                            self.config.research_recent_trades,
                            trade_after_sequence=(
                                session.last_research_trade_sequence
                                if self.config.research_trade_delta_enabled
                                else None
                            ),
                        ),
                    )
                    if self.config.research_trade_delta_enabled:
                        session.last_research_trade_sequence = (
                            session.trade_sequence
                        )

                frame_interval = (
                    self.config.replay_engaged_frame_seconds
                    if (
                        position is not None
                        or self._session_engaged(session)
                    )
                    else self.config.replay_idle_frame_seconds
                )
                if now - session.last_frame >= frame_interval:
                    session.last_frame = now
                    self.recorder.record(
                        "market_frame",
                        symbol,
                        session.frame(
                            self.config.replay_book_depth,
                            (
                                position.public()
                                if position
                                else None
                            ),
                            self.config.replay_recent_trades,
                            trade_after_sequence=(
                                session.last_replay_trade_sequence
                                if self.config.replay_trade_delta_enabled
                                else None
                            ),
                        ),
                    )
                    if self.config.replay_trade_delta_enabled:
                        session.last_replay_trade_sequence = (
                            session.trade_sequence
                        )

        async def on_message(message):
            if self.input_scopes is None:
                return await apply_message(message)
            with self.input_scopes.enter("market_message", symbol):
                return await apply_message(message)

        return on_message, fast_book_state, deep_book_state

    async def _symbol_worker(
        self,
        symbol: str,
        stop_event: asyncio.Event,
    ) -> None:
        self._transport_worker_serial += 1
        worker_id = self._transport_worker_serial
        on_message, fast_book_state, deep_book_state = self._market_handler(symbol)
        fast_depth = self.config.fast_orderbook_depth
        deep_depth = self.config.deep_orderbook_depth

        def capture_transport(event):
            self._record_input("transport", symbol, {**event, "workerId": worker_id,
                "fastState": self._transport_book_state(fast_book_state),
                "deepState": self._transport_book_state(deep_book_state)})

        await stream_symbol(
            self.config.bybit_public_ws_url,
            symbol,
            on_message,
            stop_event,
            **({"on_transport": capture_transport} if self.input_journal is not None else {}),
            fast_orderbook_depth=fast_depth,
            deep_orderbook_depth=deep_depth,
            market_queue_size=self.config.market_queue_size,
            market_queue_put_timeout_seconds=(
                self.config.market_queue_put_timeout_seconds
            ),
            market_queue_max_lag_seconds=(
                self.config.market_queue_max_lag_seconds
            ),
        )

    @staticmethod
    def _transport_book_state(state):
        return {"depth": state.depth, "synced": state.synced,
                "updateId": state.last_update_id, "seq": state.last_seq,
                "bids": [list(x) for x in sorted(state.bids.items(), reverse=True)],
                "asks": [list(x) for x in sorted(state.asks.items())]}

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
                or self.clock.time() * 1000
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

    @input_scope("evaluate", symbol_arg=True)
    async def _evaluate(self, session: ActiveSymbolSession) -> None:
        self._record_input("callback", session.symbol, {"name": "evaluate"})
        clock = self._clock_state(session)
        if clock is not None and not clock["valid"]:
            for key in self.strategies:
                session.decisions[key] = StrategyDecision(
                    strategy=key, action=Action.WAIT,
                    reasons=["Биржевое время недостоверно; новые входы запрещены"],
                    details={"state": "clock_invalid", "clockReason": clock["reason"]},
                )
            self._validate_pending_entry(session)
            return
        if not session.candles:
            return

        closed_1m = [x for x in session.candles if x.confirmed]
        closed_5m = [x for x in session.context_5m if x.confirmed]
        closed_15m = [x for x in session.context_15m if x.confirmed]
        closed_1h = [x for x in session.context_1h if x.confirmed]
        if not closed_1m:
            return

        now_ms = clock["evaluationMs"] if clock is not None else int(self.clock.time() * 1000)
        now = now_ms / 1000
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
            if not session.deep_book_is_fresh(now):
                raw_density = StrategyDecision(
                    strategy="orderbook_density",
                    action=Action.WAIT,
                    reasons=[
                        "Стакан не синхронизирован или устарел; liquidity evidence не обновляется"
                    ],
                    details={
                        "state": "stale_book",
                        "bookHealth": session.deep_book_health(now),
                        "bookSource": "deep_l1000",
                        "positionInvalidated": False,
                        "evidenceOnly": True,
                    },
                )
            else:
                density_started_ns = self.clock.perf_counter_ns()
                try:
                    with span(
                        "strategy.evaluate",
                        **{
                            "strategy.name": "orderbook_density",
                            "market.symbol": session.symbol,
                        },
                    ):
                        raw_density = density.evaluate(
                            closed_1m,
                            session.depth_orderbook(),
                            session.trend,
                            symbol=session.symbol,
                            trades=list(session.trades),
                            structure=session.structure,
                            market_context=session.market_context,
                            observed_at_ms=now_ms,
                            trade_flow=dict(trade_flow),
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
                finally:
                    observe_latency(
                        "strategy_function",
                        max(
                            0.0,
                            (
                                self.clock.perf_counter_ns()
                                - density_started_ns
                            )
                            / 1_000_000_000,
                        ),
                        strategy="orderbook_density",
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

        latency_message = session.pending_latency_message
        if latency_message is not None:
            latency_message.features_ready_mono_ns = self.clock.perf_counter_ns()
            observe_latency(
                "parse_to_features",
                max(
                    0.0,
                    (
                        latency_message.features_ready_mono_ns
                        - latency_message.parsed_mono_ns
                    )
                    / 1_000_000_000,
                ),
                stream=stream_name(latency_message.topic),
            )
            if latency_message.book_updated_mono_ns > 0:
                observe_latency(
                    "book_to_features",
                    max(
                        0.0,
                        (
                            latency_message.features_ready_mono_ns
                            - latency_message.book_updated_mono_ns
                        )
                        / 1_000_000_000,
                    ),
                    stream=stream_name(latency_message.topic),
                )
            latency_message.strategy_eval_started_mono_ns = (
                self.clock.perf_counter_ns()
            )
            observe_latency(
                "parse_to_strategy",
                max(
                    0.0,
                    (
                        latency_message.strategy_eval_started_mono_ns
                        - latency_message.parsed_mono_ns
                    )
                    / 1_000_000_000,
                ),
                stream=stream_name(latency_message.topic),
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

        scenario = self._route_scenario(session, closed_1m)
        for key, strategy in self.strategies.items():
            if key == "orderbook_density":
                continue
            if scenario is None or key != scenario.owner:
                enabled = self.strategy_enabled.get(key, False)
                decision = StrategyDecision(key, Action.WAIT,
                    ["strategy disabled" if not enabled else "not assigned to current scenario"],
                    details={"state":"disabled" if not enabled else "not_assigned",
                             "assignedOwner":scenario.owner if scenario else None})
                session.decisions[key] = decision
                self._record_decision_if_changed(session, decision)
                continue
            strategy_started_ns = self.clock.perf_counter_ns()
            try:
                with span(
                    "strategy.evaluate",
                    **{
                        "strategy.name": key,
                        "market.symbol": session.symbol,
                    },
                ):
                    decision = strategy.evaluate(
                        closed_1m,
                        session.orderbook,
                        session.trend,
                        symbol=session.symbol,
                        trades=list(session.trades),
                        structure=session.structure,
                        market_context=session.market_context,
                        observed_at_ms=now_ms,
                        trade_flow=dict(trade_flow),
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
            finally:
                observe_latency(
                    "strategy_function",
                    max(
                        0.0,
                        (
                            self.clock.perf_counter_ns()
                            - strategy_started_ns
                        )
                        / 1_000_000_000,
                    ),
                    strategy=key,
                )

            decision = self._scenario_decision(session, decision)
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
            self._scenario_prepare(session, decision)
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
                    int(self.clock.time() * 1000)
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
            "fastBookFresh": session.book_is_fresh(),
            "fastBookAgeSeconds": session.book_age_seconds(),
            "deepBookFresh": session.deep_book_is_fresh(),
            "deepBookAgeSeconds": (
                session.deep_book_age_seconds()
            ),
            "fastBookSource": "orderbook_l50",
            "deepBookSource": "orderbook_l1000",
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

        # Execution freshness must start at the first stable FIRE, not at the
        # much earlier ARMED/prepared timestamp. Otherwise a correctly patient
        # setup can become "late" merely because it waited for the confirmation
        # that the strategy itself requires.
        preparation_anchor = session.entry_freshness_anchors.get(
            strategy
        )
        fire_trigger = details.get("fireTrigger")
        fire_ms = (
            fire_trigger.get("observedAtMs")
            if isinstance(fire_trigger, dict)
            else None
        )
        fire_price = (
            fire_trigger.get("price")
            if isinstance(fire_trigger, dict)
            else None
        )
        if isinstance(fire_ms, (int, float)) and fire_ms > 0:
            resolved_fire_price = (
                float(fire_price)
                if isinstance(fire_price, (int, float))
                and fire_price > 0
                else (
                    float(decision.entry)
                    if isinstance(decision.entry, (int, float))
                    and decision.entry > 0
                    else None
                )
            )
            anchor = {
                "objectKey": object_key,
                "triggerPrice": resolved_fire_price,
                "triggerTs": float(fire_ms) / 1000,
                "source": str(
                    fire_trigger.get("source")
                    or "strategy_fire"
                ),
            }
        else:
            anchor = preparation_anchor

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

        current_price = (
            session.orderbook.mid
            or decision.entry
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
        decision.details["entryFreshness"] = freshness_public
        opportunity_public = freshness_public
        opportunity_trigger = details.get("opportunityTrigger")
        if isinstance(opportunity_trigger, dict):
            opportunity_public = classify_entry_freshness(
                decision,
                trigger_price=opportunity_trigger["price"],
                trigger_ts=opportunity_trigger["observedAtMs"] / 1000,
                current_price=float(current_price or 0.0),
                observed_ts=observed_at,
                source=opportunity_trigger["source"],
                expected_impulse_pct=opportunity_trigger["expectedImpulsePct"],
                # Waiting is not spent price movement. Signal staleness still
                # uses the independent FIRE clock in entryFreshness above.
                include_time_budget=False,
            ).public()
        decision.details["opportunityFreshness"] = opportunity_public
        if decision.tradeable:
            decision.details["causalTriggerSource"] = freshness.source
            prepared = details.get("preparedOpportunity")
            prepared_ms = (
                prepared.get("preparedAtMs")
                if isinstance(prepared, dict)
                else None
            )
            if (
                isinstance(prepared_ms, (int, float))
                and isinstance(fire_ms, (int, float))
                and prepared_ms > 0
                and fire_ms >= prepared_ms
            ):
                decision.details["armToFireSeconds"] = (
                    float(fire_ms) - float(prepared_ms)
                ) / 1000
            elif (
                preparation_anchor is not None
                and isinstance(
                    preparation_anchor.get("triggerTs"),
                    (int, float),
                )
                and isinstance(fire_ms, (int, float))
            ):
                decision.details["armToFireSeconds"] = max(
                    0.0,
                    float(fire_ms) / 1000
                    - float(preparation_anchor["triggerTs"]),
                )
            elif (
                fire_ms is None
                and freshness.confirmation_age_seconds
                is not None
            ):
                # Compatibility for older/continuation playbooks that have a
                # causal preparation anchor but don't yet emit fireTrigger.
                decision.details["armToFireSeconds"] = (
                    freshness.confirmation_age_seconds
                )

        fingerprint = (
            object_key,
            freshness.classification.value,
            opportunity_public["classification"],
            opportunity_public["triggerTs"],
            round(opportunity_public["effectiveSpentRatio"] or 0.0, 1),
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
                "opportunityFreshness": opportunity_public,
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
        decision.details["fundingSnapshot"] = (
            session.funding_public()
        )
        return self.risk.build_plan(
            session.symbol,
            decision,
            self.broker.balance,
            session.orderbook,
            self.broker.available_notional,
            self.broker.available_risk_usd,
            depth_book=session.depth_orderbook() if session.deep_book_is_fresh() else None,
            instrument=session.instrument,
            fee_schedule=session.fee_schedule,
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

    def _latency_for_selected_opportunity(
        self,
        opportunity: Opportunity,
    ) -> MarketMessage | None:
        message = self._arbiter_latency_message
        if (
            message is None
            or self._arbiter_trigger_symbol
            != opportunity.session.symbol
        ):
            return None
        setup_key = (
            opportunity.decision.strategy,
            str(opportunity.plan.setup_id or ""),
        )
        if setup_key not in self._arbiter_trigger_setups:
            return None
        return message

    @staticmethod
    def _latency_seconds(
        message: MarketMessage,
        start_attr: str,
        end_attr: str,
    ) -> float | None:
        start = int(getattr(message, start_attr, 0) or 0)
        end = int(getattr(message, end_attr, 0) or 0)
        if start <= 0 or end <= 0:
            return None
        return max(
            0.0,
            (end - start) / 1_000_000_000,
        )

    def _mark_order_sent(
        self,
        message: MarketMessage | None,
        *,
        strategy: str,
        execution_mode: str,
    ) -> None:
        if message is None:
            return
        message.order_sent_mono_ns = self.clock.perf_counter_ns()
        observe_latency(
            "fire_to_order",
            self._latency_seconds(
                message,
                "fire_mono_ns",
                "order_sent_mono_ns",
            ),
            stream=stream_name(message.topic),
            strategy=strategy,
            execution_mode=execution_mode,
        )

    def _mark_order_ack(
        self,
        message: MarketMessage | None,
        *,
        strategy: str,
        execution_mode: str,
    ) -> None:
        if message is None:
            return
        message.order_ack_mono_ns = self.clock.perf_counter_ns()
        observe_latency(
            "order_to_ack",
            self._latency_seconds(
                message,
                "order_sent_mono_ns",
                "order_ack_mono_ns",
            ),
            strategy=strategy,
            execution_mode=execution_mode,
        )

    def _mark_order_fill(
        self,
        message: MarketMessage | None,
        *,
        strategy: str,
        execution_mode: str,
    ) -> None:
        if message is None:
            return
        message.fill_mono_ns = self.clock.perf_counter_ns()
        observe_latency(
            "order_to_fill",
            self._latency_seconds(
                message,
                "order_sent_mono_ns",
                "fill_mono_ns",
            ),
            strategy=strategy,
            execution_mode=execution_mode,
        )

    def _pop_pending_order_latency(
        self,
        symbol: str,
        setup_id: str | None,
    ) -> MarketMessage | None:
        if not setup_id:
            return None
        return self._pending_order_latency.pop(
            (symbol, str(setup_id)),
            None,
        )

    @input_scope("arbiter_loop")
    async def _arbiter_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._input_sleep("arbiter", self.config.arbiter_interval_seconds)
                if self.running:
                    self._arbitrate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("arbiter_error", None, {"error": str(exc)})

    @input_scope("arbiter", symbol_arg=False)
    def _arbitrate_once(self) -> None:
        self._record_input("callback", None, {"name": "arbiter"})
        opportunities: list[Opportunity] = []
        now = self.clock.time()
        clock = self._clock_state()
        if clock is not None and not clock["valid"]:
            self._cancel_all_pending("clock_invalid")
            for session in self.sessions.values():
                session.decisions.clear()
            return
        for event in self.broker.expire_pending(now):
            self._pop_pending_order_latency(
                str(event.get("symbol") or ""),
                str(event.get("setupId") or ""),
            )
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
            if self._clock_entry_block(session):
                continue
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
            if not session.deep_book_is_fresh(now):
                continue
            if not session.confirmed_candle_is_fresh(
                self.config.confirmed_candle_stale_seconds,
                clock["evaluationMs"] / 1000 if clock is not None else now,
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

                if not self._scenario_entry_valid(session, decision):
                    continue
                base_assessment = self._owned_assessment(session, decision)
                decision.details["semanticArbitration"] = (
                    base_assessment.public()
                )
                decision.details["riskScale"] = (
                    base_assessment.risk_scale
                )
                decision.details["riskScaleSource"] = (
                    "scenario_size_policy"
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

                # The owner supplied one frozen hypothesis. Economics does not
                # send it back through another context-selection pass.
                result.plan.strategy_details["semanticArbitration"] = base_assessment.public()

                if (
                    position_action == "open"
                    and result.plan.entry_mode == "maker_limit"
                ):
                    allowed, pending_reason = (
                        self.broker.can_place_pending(
                            session.symbol
                        )
                    )
                    if not allowed:
                        self._risk_reject_if_changed(
                            session,
                            decision,
                            pending_reason,
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

            final_assessments = {row[0].strategy: row[2] for row in planned}
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
        if self._clock_entry_block(best.session):
            return
        if best.position_action == "add":
            allowed, reason = self.broker.can_add(
                best.plan
            )
        elif best.plan.entry_mode == "maker_limit":
            allowed, reason = self.broker.can_place_pending(
                best.session.symbol
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
        latency_message = (
            self._latency_for_selected_opportunity(best)
        )
        selection_payload = {
            "latencyTrace": latency_snapshot(
                latency_message
            ),
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

        self.router.submitted(best.session.symbol, self.clock.perf_counter_ns()/1e9)
        best.plan.strategy_details["scenario"] = self.router.scenarios[best.session.symbol].public()
        self._scenario_events()
        if best.plan.entry_mode == "maker_limit":
            execution_mode = "paper_maker"
            self._mark_order_sent(
                latency_message,
                strategy=best.decision.strategy,
                execution_mode=execution_mode,
            )
            with span(
                "paper.order.submit",
                **{
                    "market.symbol": best.session.symbol,
                    "strategy.name": best.decision.strategy,
                    "execution.mode": execution_mode,
                    "market.event_id": (
                        latency_message.event_id
                        if latency_message is not None
                        else None
                    ),
                },
            ):
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
            self._mark_order_ack(
                latency_message,
                strategy=best.decision.strategy,
                execution_mode=execution_mode,
            )
            if latency_message is not None:
                self._pending_order_latency[
                    (
                        best.session.symbol,
                        str(best.plan.setup_id),
                    )
                ] = latency_message
                best.plan.strategy_details[
                    "latencyTrace"
                ] = latency_snapshot(latency_message)
                selection_payload["latencyTrace"] = (
                    latency_snapshot(latency_message)
                )
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

        execution_mode = "paper_taker"
        self._mark_order_sent(
            latency_message,
            strategy=best.decision.strategy,
            execution_mode=execution_mode,
        )
        best.session.last_trade_at = now
        with span(
            "paper.order.submit",
            **{
                "market.symbol": best.session.symbol,
                "strategy.name": best.decision.strategy,
                "execution.mode": execution_mode,
                "market.event_id": (
                    latency_message.event_id
                    if latency_message is not None
                    else None
                ),
            },
        ):
            if best.position_action == "add":
                position = self.broker.add(
                    best.plan,
                    coherent_execution_book(best.session.orderbook, best.session.depth_orderbook()),
                )
            else:
                position = self.broker.open(
                    best.plan,
                    coherent_execution_book(best.session.orderbook, best.session.depth_orderbook()),
                )

        self._mark_order_ack(
            latency_message,
            strategy=best.decision.strategy,
            execution_mode=execution_mode,
        )
        self._mark_order_fill(
            latency_message,
            strategy=best.decision.strategy,
            execution_mode=execution_mode,
        )
        if latency_message is not None:
            best.plan.strategy_details[
                "latencyTrace"
            ] = latency_snapshot(latency_message)

        if best.position_action == "add":
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
        clock = self._clock_state(session)
        current = self.broker.positions.get(session.symbol)
        if clock is not None and current is not None:
            current.opened_exchange_ms = clock["evaluationMs"]
            position = current.public()
        if current is not None:
            self.router.restore_execution(session.symbol, current, self.clock.perf_counter_ns()/1e9)
        self.router.filled(session.symbol, self.clock.perf_counter_ns()/1e9)
        self._scenario_events()
        strategy_key = str(plan.get("strategy") or "")
        stats = self.strategy_stats.get(strategy_key)
        if stats is not None:
            stats["tradesOpened"] += 1
        strategy = self.strategies.get(strategy_key)
        if strategy is not None and decision is not None:
            strategy.mark_opened(session.symbol, decision)
        session.last_trade_at = self.clock.time()
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
                    "resting maker partial at planned first take; runner moves to net "
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
        session.last_trade_at = self.clock.time()
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
        s = self.router.scenarios.get(session.symbol)
        reason = self._clock_entry_block(session)
        if not self.strategy_enabled.get(strategy_key, False):
            reason = "strategy_disabled"
        elif s is not None:
            if s.owner != strategy_key:
                reason = "scenario_owner_mismatch"
            elif s.cancellation:
                reason = s.cancellation
            elif self.clock.perf_counter_ns()/1e9 >= s.expires_mono:
                reason = "scenario_expired"
            elif s.frozen:
                quote = session.orderbook.executable_entry(pending.plan.side)
                low, high = s.frozen["entryArea"]
                if quote is None or not low <= quote <= high:
                    reason = "pending_left_frozen_entry_area"
        if reason is None:
            plan = pending.plan
            probe = StrategyDecision(strategy_key, Action(plan.side.value), [],
                entry=plan.setup_entry, stop=plan.stop, target=plan.target,
                details=plan.strategy_details)
            reason = self.strategies[strategy_key].entry_invalidation(probe,session.orderbook)

        if reason is None:
            return

        event = self.broker.cancel_pending(
            session.symbol,
            f"setup_invalidated:{reason}",
        )
        if event is not None:
            self._pop_pending_order_latency(
                session.symbol,
                str(event.get("setupId") or ""),
            )
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
        clock = self._clock_state(session)
        if clock is not None and not clock["valid"]:
            return
        if (
            not pos.strategy_details.get("scenario")
            and (self.clock.perf_counter_ns() / 1e9 - pos.opened_mono
             if self.config.exchange_clock_enabled else self.clock.time() - pos.opened_at)
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
            observed_at_ms=clock["evaluationMs"] if clock is not None else int(self.clock.time() * 1000),
        )
        if not reason:
            return
        event = self.broker.close(
            session.symbol,
            session.orderbook,
            reason,
            depth_book=session.depth_orderbook() if session.deep_book_is_fresh() else None,
        )
        self._handle_broker_events(session, [event])

    def _mark_execution_from_market(
        self,
        session: ActiveSymbolSession,
        *,
        trade_ts_ms: int | None = None,
        trade_price: float | None = None,
        trade_notional_usd: float | None = None,
        trade_side: str | None = None,
    ) -> None:
        if self._clock_entry_block(session) and session.symbol in self.broker.pending_entries:
            event = self.broker.cancel_pending(session.symbol, "clock_or_receipt_invalid")
            if event:
                self._handle_broker_events(session, [event])
        self._validate_pending_entry(session)
        resolved_trade_price = (
            float(trade_price)
            if isinstance(trade_price, (int, float))
            else session.last_price
        )
        pending_events = self.broker.mark_pending(
            session.symbol,
            resolved_trade_price,
            trade_ts_ms=trade_ts_ms,
            trade_notional_usd=trade_notional_usd,
            trade_side=trade_side,
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
                latency_message = self._pop_pending_order_latency(
                    session.symbol,
                    plan_setup_id,
                )
                if latency_message is not None:
                    with span(
                        "paper.order.fill",
                        parent_span=latency_message.otel_span,
                        **{
                            "market.symbol": session.symbol,
                            "strategy.name": strategy_key,
                            "execution.mode": "paper_maker",
                            "latency.source_event_id": (
                                latency_message.event_id
                            ),
                            "latency.source_trace_id": (
                                latency_message.trace_id
                            ),
                        },
                    ):
                        self._mark_order_fill(
                            latency_message,
                            strategy=strategy_key,
                            execution_mode="paper_maker",
                        )
                    strategy_details = (
                        dict(plan.get("strategy_details") or {})
                    )
                    strategy_details[
                        "latencyTrace"
                    ] = latency_snapshot(latency_message)
                    plan["strategy_details"] = strategy_details
                    event["latencyTrace"] = (
                        latency_snapshot(latency_message)
                    )
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
                self._pop_pending_order_latency(
                    session.symbol,
                    str(event.get("setupId") or ""),
                )
                self._emit(
                    "entry_cancelled",
                    session.symbol,
                    event,
                    snapshot=True,
                )
        self._mark_position_from_book(
            session,
            trade_price=resolved_trade_price,
            trade_notional_usd=trade_notional_usd,
            trade_side=trade_side,
            observed_at_ms=trade_ts_ms,
        )

    def _mark_position_from_book(
        self,
        session: ActiveSymbolSession,
        *,
        trade_price: float | None = None,
        trade_notional_usd: float | None = None,
        trade_side: str | None = None,
        observed_at_ms: int | None = None,
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
        clock = self._clock_state(session)
        if clock is not None:
            # Funding requires reliable exchange time; stop/target management
            # continues even when the clock is invalid.
            observed_at_ms = clock["evaluationMs"] or 0
        events = self.broker.mark(
            session.symbol,
            mark,
            session.orderbook,
            depth_book=session.depth_orderbook() if session.deep_book_is_fresh() else None,
            trade_price=trade_price,
            trade_notional_usd=trade_notional_usd,
            trade_side=trade_side,
            funding_rate=session.funding_rate,
            funding_time_ms=session.next_funding_time_ms,
            funding_mark_price=(
                session.mark_price
                or mark
            ),
            observed_at_ms=(
                int(self.clock.time() * 1000)
                if observed_at_ms is None
                else observed_at_ms
            ),
        )
        self._handle_broker_events(session, events)

    def _handle_broker_events(self, session: ActiveSymbolSession, events: list[dict]) -> None:
        for event in events:
            event_type = event.get("event")
            if event_type == "partial_take":
                self._emit("partial_take", session.symbol, event, snapshot=True)
                continue
            if event_type == "funding_payment":
                self._emit(
                    "funding_payment",
                    session.symbol,
                    event,
                    snapshot=True,
                )
                continue
            if event_type == "trade_closed":
                self.router.completed(session.symbol, self.clock.perf_counter_ns()/1e9, str(event.get("reason")))
                self._scenario_events()
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
        session.cooldown_until[strategy] = self.clock.time() + self.config.setup_rearm_seconds
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
            self._pop_pending_order_latency(
                str(event.get("symbol") or ""),
                str(event.get("setupId") or ""),
            )
            self._emit(
                "entry_cancelled",
                event.get("symbol"),
                event,
            )

    def _close_all_positions(self, reason: str) -> None:
        for symbol in list(self.broker.positions):
            session = self.sessions.get(symbol)
            book = session.orderbook if session else OrderBook()
            deep_book = (
                session.depth_orderbook()
                if session
                else book
            )
            event = self.broker.close(
                symbol,
                book,
                reason,
                depth_book=deep_book,
            )
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
        self.router.reject(session.symbol, (diagnostics or {}).get("rejectionOwner", "risk"),
                           reason, self.clock.perf_counter_ns()/1e9)
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

        observed_at = (
            session.market_context.observed_at_ms / 1000
            if self.config.exchange_clock_enabled and session.market_context is not None
            else self.clock.time()
        )
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
        if event == "entry_cancelled" and symbol and hasattr(self, "router"):
            self.router.cancelled(symbol, self.clock.perf_counter_ns()/1e9, str(payload.get("reason")),
                                  setup_id=payload.get("setupId"), scenario_id=payload.get("scenarioId"))
            self._scenario_events()
        row = {"ts": self.clock.time(), "event": event, "symbol": symbol, "payload": payload}
        self.events.appendleft(row)
        stored = dict(payload)
        if snapshot and symbol in self.sessions:
            stored["market"] = self.sessions[symbol].market_snapshot()
        self.recorder.record(event, symbol, stored)
        if event in {
            "research_frame",
            "market_frame",
            "trade_opened",
            "trade_closed",
            "run_summary",
        }:
            observe_recorder_health(
                self.recorder.health()
            )

    @input_scope("public_state")
    def public_state(self, selected_symbol: str | None = None) -> dict:
        self._record_input("external", None, {"method": "public_state", "selectedSymbol": selected_symbol})
        working = list(self.sessions)
        if selected_symbol not in self.sessions:
            selected_symbol = working[0] if working else None
        market = self.sessions[selected_symbol].market_snapshot() if selected_symbol else None
        candidate_map = {x.symbol: x for x in self.candidates}
        if market is not None and selected_symbol is not None:
            market["scenarioRouting"] = self.router.public(selected_symbol)
            selected_candidate = candidate_map.get(selected_symbol)
            market["activityProfile"] = (
                selected_candidate.public() if selected_candidate else None
            )
        now = self.clock.time()
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
            "recorderHealth": self.recorder.health(),
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
