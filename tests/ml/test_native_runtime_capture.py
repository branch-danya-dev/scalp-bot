"""Production task roots on offline source adapters, never market qualification."""
import asyncio
from dataclasses import replace
import json
import time

from scalp_bot.capture import SessionRecorder
from scalp_bot.config import Settings
from scalp_bot.domain import Candidate, Candle
from scalp_bot.engine import TradingEngine
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.ml.prepared_dataset import PreparedDatasetCollector
from scalp_bot.ml.wave2 import Wave2Observer
from scalp_bot.native_dispatch import NativeDispatch
from scalp_bot.native_v5 import NativeTapeWriter, provenance, read_native_tape


class PublicSourceFixture:
    async def active_candidates(self):
        await asyncio.sleep(0)
        return [Candidate("AAAUSDT", 1e9, 1., 100.)]
    async def instrument_info(self, symbol):
        return InstrumentSpec(symbol, "Trading", .01, .001, .001, 5, 10000, 10000, 480, 10)
    async def fee_schedule(self, symbol): return None
    async def klines(self, symbol, interval, count):
        minute = int(time.time()*1000)//60000*60000
        return [Candle(minute-(100-i)*60000, 100., 100.2, 99.8, 100.05, 100., 10000., True) for i in range(100)]
    async def close(self): pass


class OfflineSocket:
    def __init__(self): self.messages = asyncio.Queue()
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def send(self, payload, **kwargs):
        for topic in json.loads(payload)["args"]:
            if topic.startswith("orderbook."):
                await self.messages.put(json.dumps(dict(topic=topic, type="snapshot", ts=int(time.time()*1000),
                    data=dict(s="AAAUSDT", b=[["100", "10000"]], a=[["100.01", "10000"]], u=1, seq=1))).encode())
    async def recv(self, **kwargs): return await self.messages.get()


async def test_real_engine_service_symbol_event_and_bybit_roots_are_owned(tmp_path):
    config = Settings(_env_file=None, exchange_clock_enabled=False, session_dir=str(tmp_path/"engine"), otel_enabled=False)
    recorder = SessionRecorder(config.session_dir)
    writer = NativeTapeWriter(tmp_path/"runtime.gz", capture_id=recorder.path.name,
        provenance=provenance(source_sha256="a"*64, config={}, runtime={}))
    dispatch = NativeDispatch(writer)
    collector = PreparedDatasetCollector(tmp_path/"prepared.jsonl")
    observer = Wave2Observer(tmp_path/"research.gz", recorder.path.name, config, {}, native_dispatch=dispatch)
    class Engine(TradingEngine):
        def _market_stream_options(self):
            return dict(super()._market_stream_options(), connect_factory=lambda *a, **kw: OfflineSocket())
    engine = None
    async def session():
        nonlocal engine
        engine = Engine(config, recorder=recorder, rest_client=PublicSourceFixture(),
            capture_inputs=True, prepared_collector=collector, research_observer=observer,
            configure_observability=False, native_dispatch=dispatch)
        try:
            await engine.start()
            async with asyncio.timeout(5):
                while not (engine.sessions["AAAUSDT"].book_synced and engine.sessions["AAAUSDT"].deep_book_synced):
                    await asyncio.sleep(.001)
            # Exercise the actual event-evaluation owner even if the flat input
            # has no naturally tradeable opportunity.
            engine._launch_event_evaluation("AAAUSDT", "offline-native-contract", 17)
            async with asyncio.timeout(5):
                while engine._event_tasks:
                    await asyncio.sleep(.001)
            assert engine.source_identity("AAAUSDT")["source_sequence"] in (1, 2)
            assert observer.failure is None
        finally:
            await engine.close()
            await observer.close(engine.clock.perf_counter_ns())
            collector.close()
    try:
        await dispatch.run(session())
        await dispatch.join()
    finally:
        writer.close()
    tape = read_native_tape(tmp_path/"runtime.gz")
    names = {r["data"]["inputs"]["name"] for r in tape.events if r["kind"] == "task_open"}
    assert {"scanner", "context", "trade-arbiter", "market-AAAUSDT", "fast-eval-AAAUSDT", "bybit-receive"} <= names
    assert tape.counts["task_open"] == tape.counts["task_end"]
    assert sum(r["kind"] == "boundary" and r["data"]["name"] == "market_enqueue" for r in tape.events) == 2
    assert sum(r["kind"] == "boundary" and r["data"]["name"] == "market_dequeue" for r in tape.events) == 2
    assert not tape.header["productionCoverage"]
