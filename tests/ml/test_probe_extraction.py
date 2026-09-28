from collections import Counter, defaultdict, deque
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace
import types

from scalp_bot.ml.probe import ShadowProbe
from test_features import context


class FakeWorker:
    def __init__(self, *args, **kwargs):
        self.ready, self.failed, self.inflight, self.latest = True, None, None, {}
        self.requests = []
    def activate(self, *args): pass
    def poll(self): return []
    def submit(self, snapshot, side, **kwargs):
        kwargs.pop("native_task", None)
        self.requests.append((asdict(snapshot), side, kwargs))
        return True


async def test_extracted_probe_matches_frozen_grid_queue_and_expiry(tmp_path, monkeypatch):
    import scalp_bot.ml.worker
    import scalp_bot.ml.shadow
    import scalp_bot.ml.probe
    monkeypatch.setattr(scalp_bot.ml.worker, "InferenceWorker", FakeWorker)
    monkeypatch.setattr(scalp_bot.ml.shadow, "ShadowAdapter", lambda *a: None)
    (tmp_path/"manifest.json").write_text("{}")
    class Clock:
        value = 1_000_000_000
        def perf_counter_ns(self): return self.value
    clock = Clock()
    monkeypatch.setattr(scalp_bot.ml.probe, "time", clock)
    legacy = types.ModuleType("probe_baseline")
    legacy.__dict__.update(Counter=Counter, deque=deque, asdict=asdict, json=json, Path=Path, time=clock)
    exec((Path(__file__).parent/"fixtures/probe_23d3525.txt").read_text(encoding="utf-8"), legacy.__dict__)

    async def run(cls):
        clock.value = 1_000_000_000
        records = []
        stages = defaultdict(list)
        async def evaluate(session): pass
        bot = SimpleNamespace(running=True, router=SimpleNamespace(epochs={"AAA":1}),
            _trade_buffer_seconds=lambda s:60, _evaluate=evaluate,
            input_journal=SimpleNamespace(sequence=7, market_message=lambda *a: None),
            recorder=SimpleNamespace(record=lambda *a: records.append(a)), source_identity=lambda s:None)
        session = SimpleNamespace(symbol="AAA", market_context=replace(context(), symbol="AAA"),
            instrument=object(), deep_book_is_fresh=lambda:True)
        probe = cls(bot, tmp_path, stages)
        bot.input_journal.market_message("AAA", SimpleNamespace(receipt_mono_ns=1_000_000_000))
        await bot._evaluate(session)
        await bot._evaluate(session)
        assert len(probe.queue) == 2  # identical 10s grid is observed only once
        probe.poll(); probe.poll()
        assert len(probe.worker.requests) == 2
        for i in range(1, 21):
            session.market_context = replace(session.market_context, observed_at_ms=120000+i*10000)
            await bot._evaluate(session)
        assert len(probe.queue) == 32 and probe.counts["queue_full"] == 8
        clock.value += 1_000_000_000  # strict TTL boundary: <, not <=
        for _ in range(32): probe.poll()
        assert probe.counts["queue_expired"] == 32
        return records, dict(stages), dict(probe.counts), probe.worker.requests
    assert await run(legacy.ShadowProbe) == await run(ShadowProbe)
