from dataclasses import replace, asdict
from types import SimpleNamespace
import json
import pytest
from scalp_bot.admission import PreparedIntent
from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine, ActiveSymbolSession
from scalp_bot.domain import OrderBook, StrategyDecision, Action
from scalp_bot.bybit import MarketMessage
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.ml.prepared_dataset import PreparedDatasetCollector, PreparedForecast, intent_key
from scalp_bot.ml.prepared_adapter import PreparedRanker
from scalp_bot.ml.contracts import SnapshotRef
from scalp_bot.ml.features import FEATURE_SCHEMA
from scalp_bot.ml.wave2 import Wave2Observer
from scalp_bot.ml.wave2_replay import replay
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_features import context


@pytest.mark.asyncio
async def test_one_capture_records_prepared_segment_maker_cross_context_and_replays(tmp_path):
    clock = ReplayRuntimeClock(wall_seconds=120., mono_ns=1_000_000_000)
    cfg = Settings(_env_file=None, exchange_clock_enabled=False, session_dir=str(tmp_path/"engine"))
    collector = PreparedDatasetCollector(tmp_path/"prepared.jsonl")
    engine = TradingEngine(cfg, clock=clock, prepared_collector=collector)
    observer = Wave2Observer(tmp_path/"research.gz", engine.recorder.path.name, cfg, {"config":"fixture"})
    engine.research_observer = observer
    engine.recorder.sequence = 17
    book = OrderBook([(100., 1000)], [(100.01, 1000)])
    session = ActiveSymbolSession("AAAUSDT", clock=clock, orderbook=book, last_price=100,
        book_synced=True, last_book_at=120., last_market_at=120., market_context=replace(context(), symbol="AAAUSDT"),
        instrument=InstrumentSpec("AAAUSDT", "Trading", .01, .001, .001, 5, 10000, 10000, 480, 10))
    engine.sessions[session.symbol] = session
    try:
        message = MarketMessage(topic="orderbook.50.AAAUSDT", ts=120000, cts=120000,
            receipt_wall_ns=120_000_000_000, receipt_mono_ns=1_000_000_000)
        observer.book(engine, session, message, fast=True)
        assert observer.failure is None
        decision = StrategyDecision("level_breakout", Action.LONG, [], entry=100.01, stop=99.5,
            target=102, setup_id="setup")
        intent = PreparedIntent("AAAUSDT", "level_breakout", "long", "setup", "scenario", "episode", 1, 100.01, 99.5, 102)
        before = dict(decision.details)
        engine._observe_prepared_intent(session, intent, decision)
        assert observer.failure is None
        assert decision.details == before and not engine.broker.positions and not engine.broker.pending_entries
        assert collector.count == 1
    finally:
        await observer.close(clock.perf_counter_ns())
        collector.close()
        await engine.close()
    result = replay(tmp_path/"research.gz", tmp_path/"replayed")
    assert result["prepared"] == 1 and result["crossVenueReplay"] == "MET"
    assert result["counts"]["maker_candidate"] == 2
    assert not result["trainingReady"]  # native label parity/primary chain still required
    from scalp_bot.research_journal import read_research
    bybit_events = [r["body"]["event"] for r in read_research(tmp_path/"research.gz")
        if r["kind"] == "venue_event" and r["body"]["event"]["venue"] == "bybit"]
    assert bybit_events[0]["sequence"] == 17


def test_ranker_identity_expiry_model_and_shadow_do_not_change_authority():
    intent = PreparedIntent("AAA", "level_breakout", "long", "s", "c", "e", 1, 100, 99, 102)
    source = SnapshotRef("capture", "AAA", 0, 1, 1, 100, "capture:capture", FEATURE_SCHEMA)
    events = []
    adapter = PreparedRanker("a"*64, lambda k,v:events.append(v), mode="enforce")
    adapter.register(intent, source)
    forecast = PreparedForecast(intent_key(intent), source, "a"*64, 150, 200, .6, .1, 20, 10, 1000)
    assert not adapter.receive(replace(forecast, model_hash="b"*64))
    assert not adapter.receive(replace(forecast, source=replace(source, source_sequence=2)))
    assert adapter.receive(forecast) and adapter.assess(intent, 160)[0]
    assert not adapter.assess(intent, 200)[0]
    assert not adapter.assess(replace(intent, side="short"), 160)[0]
    adapter.mode = "shadow"
    assert adapter.assess(intent, 200)[0]
    assert events[-1]["wouldVeto"]
