"""Offline architecture witness using production components, not qualification.

The core uses real TradingEngine callbacks with a deterministic pre-state and
no live services. The V2 capture uses the frozen Predictor and InferenceWorker
spawn/relay in capture and replay. Whole-engine scheduling remains outside this witness.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import time

from ..native_v5 import NativeTapeWriter, OwnedClock, read_native_tape, provenance, PIPELINE
from ..native_controlled import NativeControlledDriver
from ..manifest_validation import fingerprint


class _Recorder:
    def __init__(self):
        self.path = Path("native-v5-fixture")
        self.rows = []
        self.sequence = 0
    def record(self, event, symbol, payload):
        self.sequence += 1
        self.rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    def close(self): pass
    def health(self): return {}


class _NoRest:
    async def close(self): pass
    def __getattr__(self, name): raise AssertionError("offline fixture denies network: "+name)


class RecordedContext:
    def __init__(self, task):
        self.task = task
    def runtime_clock(self): return OwnedClock(self.task)
    async def clock(self, method): return getattr(self.runtime_clock(), method)()
    async def boundary(self, name, value=None): self.task.boundary(name, value)


class FixtureRuntime:
    def __init__(self, directory, *, model_dir=None):
        from ..config import Settings
        from ..cross_venue import CrossVenueRuntime
        from ..maker_shadow import MakerShadowEngine
        from .prepared_labels import PreparedLabelEngine
        self.config = Settings(_env_file=None, session_dir="native-v5-fixture", exchange_clock_enabled=False,
            event_driven_evaluation_enabled=False, otel_enabled=False)
        self.engine = None
        self.cross = CrossVenueRuntime("native-v5-fixture")
        self.research = []
        self.maker = MakerShadowEngine(self.config, self.emit)
        self.labels = PreparedLabelEngine(self.config, self.emit)
        self.model_dir = model_dir

    def emit(self, kind, body): self.research.append(deepcopy(dict(kind=kind, body=body)))

    def _clock(self, ctx):
        clock = ctx.runtime_clock()
        self.engine.clock = self.engine.broker.clock = clock
        for session in self.engine.sessions.values(): session.clock = clock
        return clock

    def ordinary(self):
        e = self.engine
        decisions = {s: {k: v.public() for k,v in session.decisions.items()} for s,session in e.sessions.items()}
        return dict(decisions=decisions,
            portfolio=dict(closed=deepcopy(e.broker.closed_trades),
                positions={k:asdict(v) for k,v in e.broker.positions.items()},
                pending={k:asdict(v) for k,v in e.broker.pending_entries.items()}),
            admissions=[r for r in e.recorder.rows if r["event"] in ("trade_opened", "trade_closed", "entry_pending")])

    async def initialize(self, ctx, inputs):
        from ..engine import TradingEngine
        from ..domain import Candle
        from ..instrument import InstrumentSpec
        from ..segment_registry import SegmentRegistry
        self.engine = TradingEngine(self.config, clock=ctx.runtime_clock(), rest_client=_NoRest(),
            recorder=_Recorder(), configure_observability=False)
        wall_ms = int(await ctx.clock("time")*1000)
        minute = wall_ms//60000*60000
        candles = [Candle(minute-(100-i)*60000, 100., 100.2, 99.8, 100.05, 100., 10000., True) for i in range(100)]
        instrument = InstrumentSpec("AAAUSDT", "Trading", .01, .001, .001, 5., 10000., 10000., 480, 10.)
        self.engine._apply_bootstrap_result("AAAUSDT", (instrument, None, candles, candles[-20:], candles[-20:], candles[-20:]))
        self.market = self.engine._market_handler("AAAUSDT")[0]
        self.registry = SegmentRegistry(self.engine.segment_expectancy)
        return {"initialized": True}

    async def market_event(self, ctx, inputs):
        from ..bybit import MarketMessage
        clock = self._clock(ctx)
        await ctx.boundary("enqueue")
        await asyncio.sleep(0)
        await ctx.boundary("dequeue")
        await ctx.boundary("dispatch")
        message = MarketMessage(**inputs["message"])
        await self.market(message)
        return {"ordinary": self.ordinary()}

    async def callback(self, ctx, inputs):
        self._clock(ctx)
        await ctx.boundary("scheduler_wake", {"callback": "evaluate"})
        await self.engine._evaluate(self.engine.sessions["AAAUSDT"])
        return {"ordinary": self.ordinary()}

    async def segment(self, ctx, inputs):
        from ..setup_segments import setup_segment
        now = int(await ctx.clock("time")*1000)
        segment = setup_segment("level_breakout", "long", {}, entry=100.01, stop=99.)
        return self.registry.assess(segment, now_ms=now)

    async def external(self, ctx, inputs):
        from ..cross_venue import VenueEvent
        from ..cross_venue_public import instruments
        await ctx.boundary("enqueue")
        await asyncio.sleep(0)
        await ctx.boundary("dequeue")
        await ctx.boundary("dispatch")
        if inputs["kind"] == "instruments":
            return {"specs": instruments(inputs["venue"], inputs["payload"])}
        now = await ctx.clock("perf_counter_ns")
        if inputs["kind"] == "gap":
            self.cross.gap(inputs["venue"], "AAAUSDT", 1, inputs["reason"])
            return {"snapshot": self.cross.snapshot("AAAUSDT", now)}
        wall = int(await ctx.clock("time")*1000)
        event = VenueEvent("native-v5-fixture", inputs["venue"], "AAAUSDT", 1, "quote",
            wall, wall, now, now, inputs["sequence"], 100., 100.01, 10., 10., units_verified=True)
        return {"accepted": self.cross.ingest(event), "snapshot": self.cross.snapshot("AAAUSDT", now)}

    async def maker_post(self, ctx, inputs):
        now = await ctx.clock("perf_counter_ns")
        session = self.engine.sessions["AAAUSDT"]
        before = len(self.research)
        self.maker.post("AAAUSDT", "long", epoch=1, now_ns=now, sequence=inputs["sequence"],
            exchange_ms=inputs["wall_ms"], book=session.orderbook, quantity=1., depth_fresh=True)
        return {"events": self.research[before:]}

    async def maker_end(self, ctx, inputs):
        now = await ctx.clock("perf_counter_ns")
        before = len(self.research)
        self.maker.invalidate("AAAUSDT", "fixture_shutdown", now)
        return {"events": self.research[before:]}

    async def label(self, ctx, inputs):
        # A real frozen label rejection, explicitly not a natural fill witness.
        now = await ctx.clock("perf_counter_ns")
        before = len(self.research)
        self.labels.add(dict(row=dict(identity="fixture-prepared", source=dict(
            capture_id="native-v5-fixture", symbol="AAAUSDT", available_mono_ns=now)),
            economicsAllowed=False, economicReason="fixture_rejection"))
        return {"events": self.research[before:]}

    async def prediction(self, ctx, inputs):
        from .native_bridge import NativeChildReplayBridge
        from .contracts import FeatureSnapshot, SnapshotRef
        from .shadow import ShadowAdapter
        ref = SnapshotRef(**inputs["ref"])
        snapshot = FeatureSnapshot(ref, tuple(inputs["names"]), tuple(inputs["values"]))
        metadata = json.loads((Path(self.model_dir)/"manifest.json").read_text())
        bridge = NativeChildReplayBridge(ctx.coordinator, self.model_dir, producer=ctx.producer,
            expected_model_hashes=inputs["modelHashes"])
        try:
            await bridge.start()
            for stage in PIPELINE[:3]:
                await ctx.boundary(stage)
            forecast = await bridge.request(ctx, snapshot, inputs["side"])
            produced = await ctx.clock("perf_counter_ns")
            _, reasons = ShadowAdapter(metadata).accept(forecast, ref, produced,
                quote=100.01, instrument=self.engine.sessions["AAAUSDT"].instrument)
            result = dict(probabilities=[forecast.p_target_first, forecast.p_stop_first, forecast.p_timeout],
                reasons=list(reasons))
            await ctx.boundary("adapter_decision", result)
            return result
        finally:
            await bridge.close()

    async def shutdown(self, ctx, inputs):
        self._clock(ctx)
        await self.engine.close()
        return {"ordinary": self.ordinary()}

    def handlers(self):
        return {("core", "initialize"):self.initialize, ("core", "market"):self.market_event,
            ("core", "callback"):self.callback, ("core", "shutdown"):self.shutdown,
            ("segment", "assess"):self.segment, ("cross_venue", "event"):self.external,
            ("cross_venue", "gap"):self.external, ("cross_venue", "instruments"):self.external,
            ("maker", "post"):self.maker_post, ("maker", "shutdown"):self.maker_end,
            ("v3", "label"):self.label, ("v2", "request"):self.prediction}


def fixture_freeze(model_dir=None):
    from ..run_manifest import code_provenance, runtime_provenance
    from ..manifest_schema import PUBLIC_CONFIG_FIELDS
    source = code_provenance(Path(__file__).resolve().parents[2])
    model = {}
    if model_dir:
        model = {name:hashlib.sha256((Path(model_dir)/name).read_bytes()).hexdigest() for name in ("model.cbm", "manifest.json")}
    script = Path(__file__).resolve().parents[2]/"scripts/check-native-v5.py"
    source["fileHashes"]["scripts/check-native-v5.py"] = hashlib.sha256(script.read_bytes()).hexdigest()
    source["sourceSha256"] = fingerprint(source["fileHashes"])
    settings = FixtureRuntime(None).config
    config = dict(fixtureVersion=1, ordinary="closed deterministic fixture, event scheduler disabled",
        publicConfig={name:getattr(settings, name) for name in sorted(PUBLIC_CONFIG_FIELDS)},
        model=model, recordingCapacityBytes=1024*1024)
    runtime = runtime_provenance()
    return dict(source=source, config=config, runtime=runtime, provenance=provenance(
        source_sha256=source["sourceSha256"], config=config, runtime=runtime))


async def capture_fixture(path, *, model_dir=None, freeze=None):
    freeze = freeze or fixture_freeze(model_dir)
    runtime = FixtureRuntime(Path(path).parent/"engine", model_dir=model_dir)
    writer = NativeTapeWriter(path, capture_id="native-v5-fixture", provenance=freeze["provenance"], capacity_bytes=1024*1024)
    worker = None
    serial = 0
    async def operation(module, operation, inputs=None, source=None):
        nonlocal serial
        serial += 1
        task = writer.endpoint(module, "parent").open_task(str(serial), operation, source=source,
            parent_task_id="1" if serial > 1 else None, inputs=inputs)
        task.start()
        try:
            result = await runtime.handlers()[(module, operation)](RecordedContext(task), inputs or {})
            task.end(outputs=result)
        except BaseException:
            task.end(reason="failed")
            raise
    try:
        await operation("core", "initialize")
        now = time.time_ns()//1_000_000
        for venue in ("binance", "okx"):
            payload = {"symbols":[dict(contractType="PERPETUAL", status="TRADING", quoteAsset="USDT",
                marginAsset="USDT", baseAsset="AAA", symbol="AAAUSDT")]} if venue == "binance" else {"data":[dict(
                    instType="SWAP", ctType="linear", settleCcy="USDT", state="live", instId="AAA-USDT-SWAP",
                    ctValCcy="AAA", ctVal="1", ctMult="1")]}
            await operation("cross_venue", "instruments", dict(kind="instruments", venue=venue, payload=payload))
            await operation("cross_venue", "gap", dict(kind="gap", venue=venue, reason="connecting"))
            await operation("cross_venue", "event", dict(kind="event", venue=venue, sequence=1))
        for sequence in range(1, 5):
            source = dict(capture_id="native-v5-fixture", symbol="AAAUSDT", epoch=1,
                source_sequence=sequence, event_id=f"m{sequence}")
            message = dict(topic="orderbook.50.AAAUSDT", type="snapshot", ts=now, cts=now,
                receipt_wall_ns=now*1_000_000, receipt_mono_ns=time.perf_counter_ns(), event_id=f"m{sequence}",
                data=dict(s="AAAUSDT", b=[["100", "1000"]], a=[["100.01", "1000"]], u=sequence, seq=sequence))
            await operation("core", "market", {"message":message}, source)
            await operation("core", "callback")
            await operation("segment", "assess")
        await operation("maker", "post", dict(sequence=4, wall_ms=now))
        await operation("v3", "label")
        if model_dir:
            from .worker import InferenceWorker
            from .contracts import FeatureSnapshot, SnapshotRef
            from .features import FEATURE_NAMES, FEATURE_SCHEMA
            from .shadow import ShadowAdapter
            metadata = json.loads((Path(model_dir)/"manifest.json").read_text())
            endpoint = writer.endpoint("v2", "parent")
            worker = InferenceWorker(model_dir, native_endpoint=endpoint)
            worker.start()
            deadline = time.monotonic()+30
            while not worker.ready and not worker.failed and time.monotonic() < deadline:
                worker.poll()
                await asyncio.sleep(.005)
            if not worker.ready: raise RuntimeError("fixture worker not ready")
            for index, side in enumerate(("long", "short")):
                source = dict(capture_id="native-v5-fixture", symbol="AAAUSDT", epoch=1,
                    source_sequence=4, event_id="m4")
                ref = SnapshotRef("native-v5-fixture", "AAAUSDT", 1, 4, now, time.perf_counter_ns(), "perf_counter", FEATURE_SCHEMA)
                snapshot = FeatureSnapshot(ref, FEATURE_NAMES, (0.,)*len(FEATURE_NAMES))
                # These are synthetic features, not a natural trading setup.
                inputs = dict(ref=asdict(ref), names=list(snapshot.names), values=list(snapshot.values), side=side,
                    modelHashes=freeze["config"]["model"])
                task = endpoint.open_task("request-"+str(index), "request", parent_task_id="1", source=source, inputs=inputs)
                task.start()
                for stage in PIPELINE[:3]: task.boundary(stage)
                worker.activate("AAAUSDT", 1)
                worker.submit(snapshot, side, native_task=task)
                deadline = time.monotonic()+5
                delivered = False
                while not delivered and not worker.failed and time.monotonic() < deadline:
                    for item in worker.poll():
                        if item[0] != "forecast": raise RuntimeError("fixture inference failed: "+str(item[1]))
                        forecast = item[1]
                        adapter_now = OwnedClock(task).perf_counter_ns()
                        _, reasons = ShadowAdapter(metadata).accept(forecast, ref, adapter_now,
                            quote=100.01, instrument=runtime.engine.sessions["AAAUSDT"].instrument)
                        outputs = dict(probabilities=[forecast.p_target_first, forecast.p_stop_first, forecast.p_timeout],
                            reasons=list(reasons))
                        worker.complete_native(task.task_id, outputs)
                        delivered = True
                    if not delivered: await asyncio.sleep(.005)
                if not delivered: raise RuntimeError("fixture request did not finish")
            worker.close()
            worker = None
        await operation("maker", "shutdown")
        await operation("core", "shutdown")
    except BaseException:
        with writer.ring.lock: writer.ring.state[4] = writer.ring.state[4] or 4
        raise
    finally:
        if worker is not None: worker.close()
        writer.close()
    tape = read_native_tape(path, expected_provenance=freeze["provenance"])
    return tape, writer.health()


async def run_fixture(output, *, model_dir=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    freeze = fixture_freeze(model_dir)
    (output/"freeze.json").write_text(json.dumps(freeze, indent=2)+"\n", encoding="utf-8")
    tape, health = await capture_fixture(output/"native-v5.gz", model_dir=model_dir, freeze=freeze)
    if fixture_freeze(model_dir)["provenance"] != freeze["provenance"]:
        raise RuntimeError("fixture source/config/runtime changed after pre-Start freeze")
    reports = []
    for repeat in range(2):
        for variant in "ABCDEF":
            runtime = FixtureRuntime(output/(variant+str(repeat)), model_dir=model_dir)
            reports.append(dict(repeat=repeat, **await NativeControlledDriver(tape, runtime.handlers()).run(variant)))
    ordinary = {row["ordinaryOperationSha256"] for row in reports}
    if len(ordinary) != 1: raise AssertionError("ordinary fixture semantics changed across A-F")
    if any(reports[i]["outputSha256"] != reports[i+6]["outputSha256"] for i in range(6)):
        raise AssertionError("fixture output did not repeat")
    critical = []
    for task in (r for r in tape.events if r["kind"] == "task_open" and r["data"]["operation"] == "request"):
        spans = [r for r in tape.events if r["task_id"] == task["task_id"] and r["kind"] == "boundary"]
        pairs = {right["data"]["name"]:(right["observed_ns"]-left["observed_ns"])/1e6 for left,right in zip(spans, spans[1:])}
        critical.append(dict(task=task["task_id"], source=task["source"], spansMs=pairs,
            totalBoundaryMs=(spans[-1]["observed_ns"]-spans[0]["observed_ns"])/1e6,
            boundaryMeaning="entry to instrumented operation; IPC spans include queue/feeder/scheduling, not isolated wire time"))
    result = dict(schema="native-v5-offline-fixture", fixture="MET", reports=reports,
        writer=health, counts=tape.counts, populationSha256=tape.population_hash,
        criticalPaths=critical, realCaptureSubprocessIPC=bool(model_dir),
        productionAtoF="NOT_TESTED", controlledW20="NOT_MET", nativeLabel="INCONCLUSIVE",
        limitations=["event_driven_evaluation disabled in fixed fixture", "no live service scheduler/source transport",
            "V3 economics rejection only; no natural fill", "synthetic V2 features, exact frozen V2 model",
            "real child request replay; whole-engine startup/poll/slice replay still unqualified",
            "coupled Wave2Observer not split into production module dispatch",
            "clock scope covers declared operations only; heartbeat/startup/codec not owned"])
    (output/"report.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result
