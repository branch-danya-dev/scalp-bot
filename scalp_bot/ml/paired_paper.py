"""Concrete offline shared-feed paper arms. No network startup or market launcher.

Captured scanner membership, bootstrap/context and market messages are applied
through current StudyEngine callbacks and ordinary AdmissionEngine/PortfolioRisk.
The logical scheduler is explicitly counterfactual, not native clock-tape parity.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from ..execution import fee_rate_for_details
from ..execution_book import coherent_execution_book
from ..experiment_controller import ArmSnapshot, ExperimentController
from ..offline_study import StudyEngine, StudyRecorder
from ..offline_benchmark import ingest
from ..offline_segment import _DeniedRest
from .prepared_adapter import PreparedRanker
from .prepared_dataset import PreparedDatasetCollector
from ..runtime_clock import ReplayRuntimeClock


class ExperimentRanker(PreparedRanker):
    """An unavailable model stops the experiment; economic veto is not failure."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, mode="enforce", **kwargs)
        self.failure = None

    def receive(self, forecast):
        valid = super().receive(forecast)
        if not valid:
            self.failure = "forecast_identity_source_or_model"
        return valid

    def assess(self, intent, now_ns):
        from .prepared_dataset import intent_key, forecast_matches
        key = intent_key(intent)
        f, source = self.forecasts.get(key), self.sources.get(key)
        if self.failure or f is None or source is None or not forecast_matches(f, intent, source, now_ns, self.model_hash):
            self.failure = self.failure or "missing_stale_or_expired_forecast"
            raise RuntimeError(self.failure)
        return super().assess(intent, now_ns)


class PaperArm:
    def __init__(self, arm_id, config, output, *, start_wall, start_ns, ranker=None):
        if arm_id not in {"A", "B"} or (arm_id == "B" and not isinstance(ranker, ExperimentRanker)):
            raise ValueError("B requires fail-closed experiment ranker")
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        self.arm_id, self.ranker = arm_id, ranker
        self.clock = ReplayRuntimeClock(wall_seconds=start_wall, mono_ns=start_ns)
        # Same basename binds both to the one shared source, with separate files.
        class ArmRecorder(StudyRecorder):
            KEEP = StudyRecorder.KEEP | {'admission_fire','prepared_forecast_decision','run_summary'}
        self.recorder = ArmRecorder(output/"shared-source.jsonl", self.clock)
        self.collector = PreparedDatasetCollector(output/"prepared.jsonl")
        self.engine = StudyEngine(config, clock=self.clock, recorder=self.recorder,
            rest_client=_DeniedRest(), configure_observability=False,
            prepared_collector=self.collector, prepared_ranker=ranker)
        self.broker = self.engine.broker
        self.handlers = {}
        self.arbiter = start_ns+int(config.arbiter_interval_seconds*1e9)
        self.disabled = False
        self.failure = None
        self.finalized = False
        self.last_sequence = -1
        self.fills = 0
        emit = self.engine._emit
        def emitted(kind, symbol, payload, **kwargs):
            if kind == "trade_opened":
                self.fills += 1
            return emit(kind, symbol, payload, **kwargs)
        self.engine._emit = emitted
        arbitrate = self.engine._arbitrate_once
        def entries():
            if not self.disabled:
                arbitrate()
        self.engine._arbitrate_once = entries

    def begin(self, start_ns):
        if self.disabled or self.finalized:
            raise RuntimeError("paper arm cannot restart")
        self.start_ns = start_ns

    def disable_entries(self):
        self.disabled = True

    async def consume(self, event):
        try:
            await self._consume(event)
        except Exception as exc:
            self.failure = self.failure or (self.ranker.failure if self.ranker else None) or type(exc).__name__+": "+str(exc)
            raise

    async def _consume(self, event):
        if event["sequence"] <= self.last_sequence:
            raise ValueError("shared event order regressed")
        self.last_sequence = event["sequence"]
        if self.recorder.health().get("writerError"):
            self.failure = "writer_failure"
            raise RuntimeError(self.failure)
        if self.ranker and self.ranker.failure:
            self.failure = self.ranker.failure
            raise RuntimeError(self.failure)
        if event["kind"] == "transport" and event["body"]["phase"] in {"fault", "cancelled"}:
            self.failure = "market_gap"
            self.disable_entries()
        if event["kind"] == "callback":
            if event["body"] != {"name":"arbiter"}:
                raise ValueError("unsupported explicit control callback")
            self.clock.set_observation(mono_ns=event["processingMonoNs"], wall_seconds=event["processingWallSeconds"])
            self.engine._arbitrate_once()
        else:
            self.arbiter = await ingest(self.engine, self.handlers, event, self.arbiter)
        if self.failure:
            raise RuntimeError(self.failure)

    def snapshot(self):
        equity = self.broker.balance
        healthy = not (self.failure or self.recorder.health().get("writerError") or
            self.ranker and self.ranker.failure)
        for symbol, pos in self.broker.positions.items():
            session = self.engine.sessions.get(symbol)
            if session is None or not session.book_is_fresh() or not session.deep_book_is_fresh():
                healthy = False
                equity = float("nan")
                break
            book = coherent_execution_book(session.orderbook, session.depth_orderbook())
            price, quantity, _ = book.exit_vwap_quantity(pos.side, pos.quantity)
            if price is None or quantity < pos.quantity-1e-10:
                healthy = False
                equity = float("nan")
                break
            sign = 1 if pos.side.value == "long" else -1
            price *= 1-sign*self.engine.config.slippage_bps/10000
            equity += sign*(price-pos.entry)*pos.quantity-pos.entry_fee_remaining
            equity -= price*pos.quantity*fee_rate_for_details(self.engine.config, "taker_market", pos.strategy_details)
        # Offline control/counterfactual fills are not native natural-fill proof.
        return ArmSnapshot(self.broker.balance, equity, 0, len(self.broker.pending_entries),
            len(self.broker.positions), self.finalized and not self.broker.positions and not self.broker.pending_entries,
            bool(healthy))

    async def finalize(self):
        self.disable_entries()
        # Use current executable depth and ordinary broker costs. Missing depth
        # remains unknown exposure rather than a fabricated zero-cost close.
        try:
            self.engine._cancel_all_pending("paired_finalization")
            for symbol in self.broker.positions:
                session = self.engine.sessions.get(symbol)
                if session is None or not session.book_is_fresh() or not session.deep_book_is_fresh():
                    raise RuntimeError("finalization_depth_unavailable")
            self.engine._stop_trading("paired_finalization")
            self.finalized = not self.broker.positions and not self.broker.pending_entries
        finally:
            self.recorder.close()
            self.collector.close()

    def ledger(self):
        return deepcopy(dict(balance=self.broker.balance, closed=self.broker.closed_trades,
            positions={k:asdict(v) for k,v in self.broker.positions.items()}, pending=list(self.broker.pending_entries)))


class SharedPaperHarness:
    def __init__(self, arm_a, arm_b, *, clock, emit):
        if arm_a.arm_id != "A" or arm_b.arm_id != "B":
            raise ValueError("ordered A/B arms required")
        self.controller = ExperimentController(arm_a, arm_b, clock=clock, emit=emit)

    async def replay(self, events, passport, hashes):
        self.controller.start(passport, hashes)
        try:
            for event in events:
                self.controller.clock.set_observation(mono_ns=event["processingMonoNs"],
                    wall_seconds=event["processingWallSeconds"])
                await self.controller.consume(event)
                if self.controller.state != "RUNNING":
                    break
        except Exception:
            self.controller.stop("shared_replay_failure")
        finally:
            if self.controller.state == "RUNNING":
                self.controller.stop("shared_replay_end")
        result = await self.controller.finalize()
        return dict(result,scope="offline_shared_feed",observedFills=[a.fills for a in self.controller.arms],
            marketPromotionAuthorized=False)
