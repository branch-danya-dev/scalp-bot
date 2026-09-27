import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.engine import ActiveSymbolSession
from test_engine_lifecycle import make_engine, book, plan


@pytest.mark.asyncio
async def test_pending_tick_does_not_run_full_strategy_graph(tmp_path):
    engine = make_engine(tmp_path, exchange_clock_enabled=False, passive_entry_enabled=True)
    try:
        pending = plan("AAAUSDT")
        pending.strategy = "weak_level_rejection"
        pending.entry_mode = "maker_limit"
        pending.market_entry = 99
        engine.broker.place_pending(pending, min_trade_ts_ms=1000)
        session = ActiveSymbolSession("AAAUSDT", orderbook=book(), last_price=100)
        engine.sessions[session.symbol] = session
        calls = []
        async def heavy(current):
            calls.append(current.symbol)
        engine._evaluate = heavy
        await engine._process_public_trade_message(session, MarketMessage(topic="publicTrade.AAAUSDT",
            data=[{"T": 1001, "p": "100", "v": "1", "S": "Buy"}]))
        assert calls == []
        assert session.symbol in engine.broker.pending_entries
    finally:
        await engine.close()
