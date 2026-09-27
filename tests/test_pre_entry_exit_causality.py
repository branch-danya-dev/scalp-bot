"""A delayed batch received before entry cannot fill later resting exits."""
import pytest
from test_engine_lifecycle import make_engine,book,plan
from scalp_bot.engine import ActiveSymbolSession

@pytest.mark.asyncio
async def test_pre_entry_received_trade_cannot_fill_resting_target(tmp_path):
    engine=make_engine(tmp_path,exchange_clock_enabled=False,partial_take_enabled=False)
    try:
        s=ActiveSymbolSession(symbol='AAAUSDT',orderbook=book(),book_synced=True,last_price=100)
        engine.sessions[s.symbol]=s
        p=plan(s.symbol);p.strategy='level_breakout';p.target=101
        pos=engine.broker.open(p,book())
        s.trade_receipt_mono=pos.opened_mono-.03
        engine._mark_execution_from_market(s,trade_ts_ms=int(pos.opened_at*1000)-1,trade_price=102,trade_notional_usd=1e6,trade_side='Buy')
        assert s.symbol in engine.broker.positions, 'pre-entry batch retroactively filled a later maker exit'
        assert pos.maker_target_trade_notional_usd==0
        s.trade_receipt_mono=pos.opened_mono+.001
        engine._mark_execution_from_market(s,trade_ts_ms=int(pos.opened_at*1000)+1,trade_price=102,trade_notional_usd=1e6,trade_side='Buy')
        assert s.symbol not in engine.broker.positions
        assert engine.broker.closed_trades[-1]['reason']=='target'
    finally:await engine.close()

@pytest.mark.asyncio
async def test_pre_entry_batch_does_not_disable_protective_stop(tmp_path):
    engine=make_engine(tmp_path,exchange_clock_enabled=False,partial_take_enabled=False)
    try:
        s=ActiveSymbolSession(symbol='AAAUSDT',orderbook=book(),book_synced=True,last_price=100);engine.sessions[s.symbol]=s
        p=plan(s.symbol);p.strategy='level_breakout';pos=engine.broker.open(p,book())
        s.trade_receipt_mono=pos.opened_mono-.03;s.orderbook=book(98,98.02)
        engine._mark_execution_from_market(s,trade_ts_ms=int(pos.opened_at*1000)-1,trade_price=102,trade_notional_usd=1e6,trade_side='Buy')
        assert not engine.broker.positions
        assert engine.broker.closed_trades[-1]['reason']=='stop'
    finally:await engine.close()
