import asyncio
from time import perf_counter_ns

import pytest

from scalp_bot.bybit import _receive_or_processor_failure, _process_market_queue, MarketDataBackpressureError, OrderBookState


async def test_consumer_failure_interrupts_silent_recv_and_leaves_no_receive_task():
    cancelled = asyncio.Event()
    class Socket:
        async def recv(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    async def fail():
        await asyncio.sleep(.01)
        raise MarketDataBackpressureError("consumer lag")
    task = asyncio.create_task(fail())
    with pytest.raises(MarketDataBackpressureError, match="consumer lag"):
        await asyncio.wait_for(_receive_or_processor_failure(Socket(), task), .5)
    assert cancelled.is_set() and task.done()


async def test_reconnect_clears_book_and_preparation_without_capture(tmp_path):
    from scalp_bot.config import Settings
    from scalp_bot.engine import TradingEngine, ActiveSymbolSession
    from scenario_support import assigned
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path), exchange_clock_enabled=False))
    try:
        session = ActiveSymbolSession("AAA", book_synced=True, deep_book_synced=True)
        engine.sessions["AAA"] = session
        scenario = assigned(engine, session, "level_breakout")
        fast, deep = OrderBookState(50), OrderBookState(1000)
        fast.synced = deep.synced = True
        engine._invalidate_transport("AAA", {"phase":"fault", "topics":["orderbook.50.AAA"]}, fast, deep)
        assert not fast.synced and not session.book_synced
        assert deep.synced and session.deep_book_synced
        assert scenario.state == "INVALIDATED"
        with pytest.raises(Exception, match="before a fresh snapshot"):
            fast.apply({"type":"delta", "data":{"u":2,"b":[],"a":[]}})
    finally:
        await engine.close()
