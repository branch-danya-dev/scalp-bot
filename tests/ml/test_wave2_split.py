"""All-on parity against the exact combined observer before the split."""
from dataclasses import replace
from pathlib import Path
import types

from scalp_bot.admission import PreparedIntent
from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import OrderBook, StrategyDecision, Action, TradeTick
from scalp_bot.engine import TradingEngine, ActiveSymbolSession
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.ml.prepared_dataset import PreparedDatasetCollector
from scalp_bot.ml.wave2 import Wave2Observer
from scalp_bot.research_journal import read_research
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_features import context


def legacy_observer():
    module = types.ModuleType("scalp_bot.ml._combined_baseline")
    module.__package__ = "scalp_bot.ml"
    source = (Path(__file__).parent/"fixtures/wave2_combined_23d3525.txt").read_text(encoding="utf-8-sig")
    exec(compile(source, "exact-23d3525-wave2", "exec"), module.__dict__)
    return module.Wave2Observer


async def witness(path, observer_type):
    clock = ReplayRuntimeClock(wall_seconds=120., mono_ns=1_000_000_000)
    calls = []
    class Clock:
        def time(self): calls.append("time"); return clock.time()
        def monotonic(self): calls.append("monotonic"); return clock.monotonic()
        def perf_counter_ns(self): calls.append("perf_counter_ns"); return clock.perf_counter_ns()
    observed = Clock()
    cfg = Settings(_env_file=None, exchange_clock_enabled=False, session_dir=str(path/"engine"))
    collector = PreparedDatasetCollector(path/"prepared.jsonl")
    engine = TradingEngine(cfg, clock=observed, prepared_collector=collector, configure_observability=False)
    engine.recorder.path = Path("same-capture")
    observer = observer_type(path/"research.gz", "same-capture", cfg, {"config":"fixture"})
    engine.research_observer = observer
    engine.recorder.sequence = 17
    book = OrderBook([(100., 1000)], [(100.01, 1000)])
    session = ActiveSymbolSession("AAAUSDT", clock=observed, orderbook=book, last_price=100,
        book_synced=True, last_book_at=120., last_market_at=120., market_context=replace(context(), symbol="AAAUSDT"),
        instrument=InstrumentSpec("AAAUSDT", "Trading", .01, .001, .001, 5, 10000, 10000, 480, 10))
    engine.sessions[session.symbol] = session
    try:
        message = MarketMessage(topic="orderbook.50.AAAUSDT", ts=120000, cts=120000,
            receipt_wall_ns=120_000_000_000, receipt_mono_ns=1_000_000_000)
        observer.book(engine, session, message, fast=True)
        observer.book(engine, session, message, fast=False)
        decision = StrategyDecision("level_breakout", Action.LONG, [], entry=100.01, stop=99.5,
            target=102, setup_id="setup")
        intent = PreparedIntent("AAAUSDT", "level_breakout", "long", "setup", "scenario", "episode", 1, 100.01, 99.5, 102)
        engine._observe_prepared_intent(session, intent, decision)
        observer.trade(engine, session, message, TradeTick(120000, 100., 1., "Buy", sequence=1))
        observer.context(engine, session)
        observer.closed_position(engine, session, dict(strategy="level_breakout", side="long",
            entry=100.01, initialStop=99.5, reason="stop", netPnl=-1., initialRiskUsd=1.,
            setupId="setup", openedAt=120., closedAt=121.))
        observer.gap(engine, session.symbol, "test_gap")
        assert observer.failure is None
    finally:
        await observer.close(observed.perf_counter_ns())
        collector.close()
        await engine.close()
    return [dict(kind=r["kind"], body=r["body"]) for r in read_research(path/"research.gz")], calls


async def test_all_on_split_preserves_combined_outputs_and_clock_call_order(tmp_path):
    old, new = tmp_path/"old", tmp_path/"new"
    old.mkdir(); new.mkdir()
    assert await witness(old, legacy_observer()) == await witness(new, Wave2Observer)
