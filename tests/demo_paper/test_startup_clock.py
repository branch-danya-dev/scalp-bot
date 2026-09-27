"""Replay the two actual startup clock samples without market/account requests."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from .test_contracts import make_pair, plan
from scalp_bot.demo_paper.contracts import Intent
from scalp_bot.demo_paper.engine import PairedEngine
from scalp_bot.demo_paper.runtime import Session
from scalp_bot.domain import OrderBook
from scalp_bot.engine import ActiveSymbolSession
from scalp_bot.offline_segment import _DeniedRest
from scalp_bot.recorder import SessionRecorder
from scalp_bot.runtime_clock import ReplayRuntimeClock


def fixture_engine(tmp_path):
    samples=json.loads((Path(__file__).parent/'fixtures/startup_clock_samples.json').read_text())['samples']
    first=samples[0]['sample']
    clock=ReplayRuntimeClock(mono_ns=int(first['received_mono']*1e9),wall_seconds=first['received_wall_ms']/1000)
    portfolio,arms,spec,events=make_pair()
    cfg=portfolio.config.model_copy(update={'exchange_clock_enabled':True,'session_dir':str(tmp_path)})
    engine=PairedEngine(cfg,portfolio,None,None,clock=clock,rest_client=_DeniedRest(),
        recorder=SessionRecorder(str(tmp_path),clock=clock),configure_observability=False)
    fake=SimpleNamespace(engine=engine,portfolio=portfolio,stop=asyncio.Event(),private_ready=asyncio.Event(),
                         _entry_readiness_key=None,emit=portfolio.emit)
    fake.private_ready.set()
    return engine,fake,clock,samples,arms,spec,events


async def test_recorded_rejected_first_sync_waits_then_recovers_without_new_start(tmp_path):
    engine,session,clock,samples,arms,spec,events=fixture_engine(tmp_path)
    try:
        assert engine._apply_clock_sample(samples[0]['sample']) is False
        assert engine._clock_state()['reason']=='sync_rtt_exceeded'
        market=ActiveSymbolSession('BTCUSDT',clock=clock,receipt_clock_required=True)
        market.fast_receipt_mono=clock.perf_counter_ns()/1e9
        engine.sessions[market.symbol]=market
        original_receipt=market.fast_receipt_mono
        engine._arbitrate_once()
        assert session.portfolio.stop_reason is None
        assert not session.portfolio.accepting
        Session.refresh_admission(session)
        assert not session.portfolio.can_open('BTCUSDT')[0]
        assert not any(a.venue.orders for a in arms.values())
        second=samples[1]['sample']
        clock.set_observation(mono_ns=int(second['received_mono']*1e9),wall_seconds=second['received_wall_ms']/1000)
        assert engine._apply_clock_sample(second) is True
        Session.refresh_admission(session)
        assert session.portfolio.accepting and session.portfolio.stop_reason is None
        assert market.fast_receipt_mono==original_receipt
        assert market.book_age_seconds()>16  # Clock recovery cannot freshen market data.
        assert engine._clock_entry_block(market)=='clock:stale_book_receipt'
        assert [p['accepting'] for e,p in events if e=='entry_readiness']==[False,True]
        session.portfolio.halt('operator_stop')
        Session.refresh_admission(session)
        assert not session.portfolio.accepting  # A valid clock cannot undo Stop.
    finally:await engine.close()


@pytest.mark.parametrize("filled",[False,True])
async def test_clock_loss_with_reserved_intent_still_halts_and_cancels_unsent(tmp_path,filled):
    engine,session,clock,samples,arms,spec,events=fixture_engine(tmp_path)
    tasks=[]
    try:
        intent=Intent.freeze('r',plan(),1,clock.perf_counter_ns())
        assert session.portfolio.admit(intent,spec)[0]
        if filled:
            tasks=[asyncio.create_task(session.portfolio.dispatch(name)) for name in arms]
            await asyncio.sleep(.01)
            assert all(a.remaining('BTCUSDT')==1 for a in arms.values())
        engine._arbitrate_once()
        assert session.portfolio.stop_reason=='clock_invalid'
        if filled:
            await session.portfolio.manage_once()
            assert all(a.remaining('BTCUSDT')==0 for a in arms.values())
            assert all(list(a.venue.orders.values())[-1].command.reduce_only for a in arms.values())
        else:
            tasks=[asyncio.create_task(session.portfolio.dispatch(name)) for name in arms]
            await asyncio.sleep(0)
            assert not any(a.venue.orders for a in arms.values())
        for name in arms:session.portfolio.queues[name].put_nowait(None)
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:task.cancel()
        await engine.close()
