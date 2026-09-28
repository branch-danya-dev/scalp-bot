"""Frozen V2 shadow/load actor; no authority to submit trading orders."""
import asyncio
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import time

class ShadowProbe:
    """Frozen V2 load probe only; adapter proposals never reach admission."""
    def __init__(self, bot, model, stages, *, native_dispatch=None):
        from scalp_bot.ml.worker import InferenceWorker
        from scalp_bot.ml.shadow import ShadowAdapter
        self.bot, self.stages = bot, stages
        self.native_dispatch = native_dispatch
        self.clock = native_dispatch.clock() if native_dispatch is not None else time
        self.pending_native = {}
        self.endpoint = native_dispatch.writer.endpoint("v2", "parent") if native_dispatch is not None else None
        self.worker = InferenceWorker(model, trace=self._timed, native_endpoint=self.endpoint, clock=self.clock)
        self.adapter = ShadowAdapter(json.loads((Path(model)/'manifest.json').read_text()))
        self.source, self.current, self.grid = {}, {}, {}
        self.queue = deque(maxlen=32)
        self.counts = Counter()
        self.model = model
        original = bot.input_journal.market_message
        def recorded(symbol, message):
            original(symbol, message)
            if message.receipt_mono_ns:
                identity = bot.source_identity(symbol)
                self.source[symbol] = (identity["source_sequence"] if identity else bot.input_journal.sequence, message.receipt_mono_ns)
        bot.input_journal.market_message = recorded
        evaluate = bot._evaluate
        async def evaluated(session):
            await evaluate(session)
            with self._scope("evaluate"):
                if not bot.running or not self.worker.ready or not session.market_context or not session.instrument:
                    return
                source = self.source.get(session.symbol)
                if source is None:
                    return
                context = session.market_context
                grid = context.observed_at_ms//10_000
                if self.grid.get(session.symbol) == grid:
                    return
                self.grid[session.symbol] = grid
                from scalp_bot.ml.contracts import SnapshotRef
                from scalp_bot.ml.features import FEATURE_SCHEMA, ContextCoverage, extract_context_features
                before = self.clock.perf_counter_ns()
                ref = SnapshotRef(native_dispatch.ingress.capture_id if native_dispatch is not None else 'technical-smoke-v2-shadow', session.symbol,
                    bot.router.epochs.get(session.symbol, 0), source[0], context.observed_at_ms,
                    source[1], 'perf_counter', FEATURE_SCHEMA)
                covered = bot._trade_buffer_seconds(session)
                coverage = ContextCoverage(tuple(n for n in (5,15,60) if covered >= n), (),
                    context.forming_candle is not None, session.deep_book_is_fresh(),
                    context.structure is not None, True)
                snapshot = extract_context_features(context, ref, coverage)
                after = self.clock.perf_counter_ns()
                stages['features'].append((after-before)/1e6)
                stages['data_to_features'].append((after-ref.available_mono_ns)/1e6)
                self.current[session.symbol] = ref
                self.worker.activate(session.symbol, ref.selection_epoch)
                for side in ('long','short'):
                    native = self._request(snapshot, side)
                    if native is not None:
                        native.boundary("features_ready")
                    if len(self.queue) == self.queue.maxlen:
                        self.counts['queue_full'] += 1
                        if native is not None:
                            native.end(reason="dropped", outputs={"reason":"probe_queue_full"})
                            self.pending_native.pop(native.task_id)
                    else:
                        self.queue.append((snapshot, side, after, native))
                        if native is not None:
                            native.boundary("probe_enqueue")
                bot.recorder.record('shadow_features', session.symbol, dict(source=asdict(ref),
                    features=snapshot.values, featureEndNs=after))
        bot._evaluate = evaluated

    def _timed(self, row):
        # Completed forecasts are emitted after the adapter's decision below.
        if row['terminal'] != 'forecast':
            self.bot.recorder.record('pipeline_worker', row['identity'][1], row)
            native = row.get("nativeTask")
            if native is not None:
                self.pending_native.pop(native["task_id"], None)

    def _scope(self, name):
        return self.native_dispatch.scope("v2", "probe:"+name) if self.native_dispatch is not None else nullcontext()

    def _request(self, snapshot, side):
        if self.endpoint is None:
            return None
        ref = snapshot.ref
        source = self.bot.source_identity(ref.symbol)
        task = self.endpoint.open_task(f"probe:{ref.symbol}:{ref.selection_epoch}:{ref.source_sequence}:{side}",
            "request", parent_task_id=self.native_dispatch.owner().task_id, source=source,
            inputs=dict(ref=asdict(ref), names=list(snapshot.names), values=list(snapshot.values), side=side))
        task.start()
        self.pending_native[task.task_id] = task
        return task

    def poll(self):
        with self._scope("poll"):
            self._poll()

    def _poll(self):
        for item in self.worker.poll():
            received = self.clock.perf_counter_ns()
            if item[0] != 'forecast':
                self.counts['worker_error:'+str(item[1])] += 1
                continue
            forecast = item[1]
            self.counts['forecasts'] += 1
            self.stages['prediction'].append(item[2]/1e6)
            session = self.bot.sessions.get(forecast.source.symbol)
            if session is None:
                reasons = ('symbol_inactive',)
            else:
                from scalp_bot.domain import Side
                _, reasons = self.adapter.accept(forecast,
                    self.current.get(session.symbol, forecast.source), received,
                    quote=session.orderbook.executable_entry(Side(forecast.side)), instrument=session.instrument)
            terminal = self.clock.perf_counter_ns()
            if self.endpoint is not None:
                task_id = item[-1]["pipelineTiming"]["nativeTask"]["task_id"]
                self.worker.complete_native(task_id, dict(forecast=asdict(forecast), reasons=list(reasons)))
                self.pending_native.pop(task_id)
            if isinstance(item[-1], dict) and 'pipelineTiming' in item[-1]:
                timing=dict(item[-1]['pipelineTiming'],adapter_end_ns=terminal)
                self.bot.recorder.record('pipeline_worker', forecast.source.symbol,timing)
            self.stages['data_to_adapter'].append((terminal-forecast.source.available_mono_ns)/1e6)
            self.counts.update(reasons or ('shadow_proposal_never_executed',))
            self.bot.recorder.record('shadow_forecast', forecast.source.symbol,
                dict(forecast=asdict(forecast), reasons=reasons, receivedNs=received, adapterEndNs=terminal))
        if self.worker.ready and not self.worker.inflight and not self.worker.latest and self.queue:
            snapshot, side, feature_end, native = self.queue.popleft()
            if native is not None:
                native.boundary("probe_dequeue")
            if self.clock.perf_counter_ns()-snapshot.ref.available_mono_ns < 1_000_000_000:
                accepted = self.worker.submit(snapshot, side, feature_ready_ns=feature_end,
                    probe_queued_ns=feature_end,probe_queue_depth=len(self.queue), native_task=native)
                self.counts['submitted' if accepted else 'submit_rejected'] += 1
                if not accepted and native is not None:
                    self.pending_native.pop(native.task_id, None)
            else:
                self.counts['queue_expired'] += 1
                if native is not None:
                    native.end(reason="expired", outputs={"reason":"probe_queue_expired"})
                    self.pending_native.pop(native.task_id)
                self.bot.recorder.record('pipeline_worker',snapshot.ref.symbol,dict(
                    identity=[snapshot.ref.capture_id,snapshot.ref.symbol,snapshot.ref.selection_epoch,snapshot.ref.source_sequence,side],
                    available_ns=snapshot.ref.available_mono_ns,feature_ready_ns=feature_end,
                    terminal='probe_queue_expired',terminal_ns=self.clock.perf_counter_ns()))

    def start(self):
        with self._scope("start"):
            self.worker.start()

    async def wait_ready(self):
        with self._scope("startup"):
            deadline = self.clock.monotonic()+30
            while not self.worker.ready and not self.worker.failed and self.clock.monotonic()<deadline:
                await asyncio.sleep(.01)
            if not self.worker.ready:
                raise RuntimeError(self.worker.failed or "shadow worker startup timeout")

    async def heartbeat(self, stop, research=None):
        while not stop.is_set():
            with self._scope("heartbeat"):
                before = self.clock.perf_counter_ns()
                await asyncio.sleep(.005)
                self.stages["event_loop_lateness"].append(max(0,
                    (self.clock.perf_counter_ns()-before-5_000_000)/1e6))
                self.poll()
            if research is not None:
                research.watch(tuple(self.bot.sessions))

    def close(self):
        with self._scope("close"):
            queued = list(self.queue)
            self.queue.clear()
            self.worker.close()
            # Worker owns submitted requests; probe owns queued requests.
            for _, _, _, native in queued:
                if native is not None:
                    native.end(reason="shutdown", outputs={"reason":"probe_queue_shutdown"})
            self.pending_native.clear()
