import json
from types import SimpleNamespace

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Side
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.paper import PaperBroker
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_paper import book, plan


def test_replay_clock_preserves_wall_jumps_and_rejects_reordering_atomically():
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    clock.set_observation(wall_seconds=900, mono_ns=11_000_000_000)
    assert clock.time() == 900
    assert clock.monotonic() == 11
    for wall, mono in [(800, 9_000_000_000), (float('nan'), 12_000_000_000),
                       (800, 1.5), (800, True)]:
        with pytest.raises(ValueError):
            clock.set_observation(wall_seconds=wall, mono_ns=mono)
        assert (clock.time(), clock.perf_counter_ns()) == (900, 11_000_000_000)
    clock.set_observation(wall_seconds=901, mono_ns=11_000_000_000)
    assert clock.time() == 901


def forbid_os_clock(monkeypatch):
    def forbidden():
        raise AssertionError('replay consulted OS clock')
    monkeypatch.setattr('scalp_bot.runtime_clock.time', SimpleNamespace(
        time=forbidden, monotonic=forbidden, perf_counter_ns=forbidden))
    # Position/PendingEntry legacy defaults must also be bypassed by the broker.
    monkeypatch.setattr('scalp_bot.paper.perf_counter_ns', forbidden)


def test_same_paper_execution_path_is_repeatable_without_os_time(monkeypatch):
    cfg = Settings(_env_file=None, exchange_clock_enabled=True,
                   taker_fee_rate=.0005, slippage_bps=0, partial_take_enabled=False,
                   no_follow_through_seconds=5, passive_entry_timeout_seconds=5,
                   max_open_positions=4, max_leverage=2)
    forbid_os_clock(monkeypatch)

    def replay():
        clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
        broker = PaperBroker(cfg, clock=clock)
        pos = broker.open(plan('AAAUSDT', Side.LONG), book(99.99, 100))
        pending_plan = plan('BBBUSDT', Side.LONG)
        pending_plan.entry_mode = 'maker_limit'
        pending = broker.place_pending(pending_plan)
        assert pos.opened_mono == pending.created_mono == 10
        clock.set_observation(wall_seconds=100_000, mono_ns=11_000_000_000)
        assert broker.expire_pending() == []
        assert broker.mark('AAAUSDT', 99.7, book(99.7, 99.71)) == []
        clock.set_observation(wall_seconds=900, mono_ns=16_000_000_000)
        cancelled = broker.expire_pending()
        closed = broker.mark('AAAUSDT', 99.7, book(99.7, 99.71))
        assert cancelled[0]['reason'] == 'passive_entry_timeout'
        assert closed[0]['reason'] == 'no_follow_through'
        assert closed[0]['openedAt'] == 1000 and closed[0]['closedAt'] == 900
        assert closed[0]['fees'] > 0
        assert closed[0]['netPnl'] == pytest.approx(closed[0]['grossPnl'] - closed[0]['fees'])
        return cancelled, closed, broker.balance

    assert replay() == replay()


def test_replay_partial_take_and_stop_use_injected_timestamps(monkeypatch):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    broker = PaperBroker(Settings(_env_file=None, exchange_clock_enabled=True,
        taker_fee_rate=.0005, slippage_bps=0, partial_take_at_r=1,
        partial_take_fraction=.7, runner_target_r=2.5, no_follow_through_seconds=999), clock=clock)
    forbid_os_clock(monkeypatch)
    broker.open(plan('AAAUSDT', Side.LONG, 1000), book(99.99, 100))
    clock.set_observation(wall_seconds=1001, mono_ns=11_000_000_000)
    events = broker.mark('AAAUSDT', 100.55, book(100.55, 100.56))
    assert events[0]['event'] == 'partial_take'
    pos = broker.positions['AAAUSDT']
    assert pos.partial_taken_at == 1001
    clock.set_observation(wall_seconds=1002, mono_ns=12_000_000_000)
    events = broker.mark('AAAUSDT', pos.stop - .01, book(pos.stop - .01, pos.stop))
    assert events[-1]['event'] == 'trade_closed'
    assert events[-1]['closedAt'] == 1002
    assert events[-1]['partialTakenAt'] == 1001
    assert broker.total_closed_trades == 1


@pytest.mark.asyncio
async def test_engine_clock_expiry_cancels_pending_and_recovers_on_same_clock(tmp_path, monkeypatch):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    cfg = Settings(_env_file=None, session_dir=str(tmp_path), exchange_clock_enabled=True,
                   taker_fee_rate=0, slippage_bps=0)
    engine = TradingEngine(cfg, clock=clock)
    try:
        forbid_os_clock(monkeypatch)
        assert engine.broker.clock is clock and engine.recorder.clock is clock
        session = ActiveSymbolSession(symbol='AAAUSDT', clock=clock, receipt_clock_required=True,
                                      fast_receipt_mono=10, deep_receipt_mono=10)
        engine.sessions[session.symbol] = session
        assert session.activated_at == session.last_ranked_at == 1000
        assert session.book_age_seconds() == 0
        assert engine.market_clock.synchronize(server_ms=1_000_000, sent_mono=9.99,
                                               received_mono=10, received_wall_ms=1_000_000)
        assert engine._clock_state()['valid']
        p = plan('AAAUSDT', Side.LONG)
        p.entry_mode = 'maker_limit'
        engine.broker.place_pending(p)
        clock.set_observation(wall_seconds=1061, mono_ns=71_000_000_000)
        assert session.book_age_seconds() == 61
        assert engine._clock_state()['reason'] == 'synchronization_expired'
        engine._arbitrate_once()
        assert not engine.broker.pending_entries and not engine.broker.positions
        assert engine.events[0]['payload']['reason'] == 'clock_invalid'
        assert engine.events[0]['ts'] == 1061
        recorded = [json.loads(line) for line in engine.recorder.path.read_text(encoding='utf8').splitlines()]
        assert recorded[-1]['ts'] == 1061
        assert recorded[-1]['iso'] == '1970-01-01T00:17:41+00:00'
        assert engine.market_clock.synchronize(server_ms=1_061_000, sent_mono=70.99,
                                               received_mono=71, received_wall_ms=1_061_000)
        assert engine._clock_state()['valid']
        # Clock recovery must not refresh source receipt timestamps.
        assert session.book_age_seconds() == 61
    finally:
        await engine.close()
