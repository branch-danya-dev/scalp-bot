"""Service/portfolio integration with controlled and unmodified breakout signals.

Synthetic market paths validate parity, not strategy profitability.
"""
import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candidate, Candle, StrategyDecision
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_input_journal import write_report


def install_signal(engine):
    def evaluate(*args, **kwargs):
        return StrategyDecision(strategy='trend_structure', action=Action.LONG,
            reasons=['controlled integration signal'], confidence=.9, entry=100,
            stop=99.5, target=100.8, watched_level=99.8, setup_id='portfolio-fixture',
            details={'setupQuality': .9})
    engine.strategies['trend_structure'].evaluate = evaluate


async def fixture(tmp_path, monkeypatch, exit_kind='shutdown', production=False,
                  file_capture=False, event_driven=False, beta=False, capture_factory=None):
    clock = ReplayRuntimeClock(wall_seconds=4805, mono_ns=10**10)
    recorder = None
    if file_capture and capture_factory is None:
        from scalp_bot.capture import CaptureRecorder
        recorder = CaptureRecorder(str(tmp_path), clock=clock)
    config = Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=True, event_driven_evaluation_enabled=event_driven,
        trend_structure_enabled=not production, weak_level_rejection_enabled=False,
        density_enabled=False, breakout_enabled=production, partial_take_enabled=False,
        paper_run_duration_seconds=61 if exit_kind == 'duration_elapsed' else 14400,
        working_symbols=1, min_net_profit_usd=0, min_net_profit_equity_fraction=0,
        min_net_reward_risk=0, absolute_min_net_reward_risk=0,
        max_leverage=1, risk_fraction=.01)
    capture = capture_factory(config, clock) if capture_factory else None
    live = capture.engine if capture else TradingEngine(
        config, clock=clock, capture_inputs=True, recorder=recorder)
    prefix = [] if file_capture else [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
                                    if json.loads(line)['event'] == 'replay_input']
    if not production:
        install_signal(live)
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    if not file_capture:
        live.recorder.record = live.input_journal.record = record
        live.recorder.start_background_writer = lambda: None
    if beta:
        live.toggle_strategy('price_action_hypothesis', True)
    async def candidates(): return [Candidate('AAA', 2e8, .02, 100, activity_rank=1)]
    async def metadata(symbol): return None
    async def candles(symbol, interval, limit):
        if production:
            from test_strategies import mature_breakout_candles
            return mature_breakout_candles() if interval == '1' else []
        return [Candle(4740000, 100, 101, 99, 100, 10, 1000)]
    async def sample():
        return dict(server_ms=4805000, sent_mono=9.99, received_mono=10, received_wall_ms=4805000)
    live.rest.active_candidates = candidates
    live.rest.instrument_info = live.rest.fee_schedule = metadata
    live.rest.klines = candles
    live.rest.clock_sample = sample
    timer_wake = asyncio.Event()
    async def wait(delay):
        if exit_kind == 'duration_elapsed' and delay == live.config.paper_run_duration_seconds:
            await timer_wake.wait()
        else:
            await asyncio.Event().wait()
    live._periodic_sleep = wait
    queue = asyncio.Queue()
    async def stream(url, symbol, callback, stop, **kwargs):
        def notify(phase):
            kwargs['on_transport'](dict(phase=phase, attempt=1,
                topics=['orderbook.50.AAA', 'orderbook.1000.AAA', 'publicTrade.AAA', 'kline.1.AAA'],
                errorType=None, discarded=0))
        notify('connecting'); notify('subscription_sent')
        try:
            while True:
                message, done = await queue.get()
                try:
                    await callback(message)
                    done.set_result(None)
                except Exception as exc:
                    done.set_exception(exc)
        except asyncio.CancelledError:
            notify('cancelled')
            raise
        finally:
            notify('drained')
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', stream)
    async def send(message):
        message.receipt_mono_ns = clock.perf_counter_ns()
        done = asyncio.get_running_loop().create_future()
        queue.put_nowait((message, done))
        await asyncio.wait_for(done, timeout=3)
    await live.start()
    for depth in (50, 1000):
        await send(MarketMessage(topic=f'orderbook.{depth}.AAA', type='snapshot', ts=4805000,
            data={'u': 1, 'seq': 1, 'b': [[str(100.16 if production else 99.99), '1000']], 'a': [[str(100.17 if production else 100.01), '1000']]}))
    await send(MarketMessage(topic='publicTrade.AAA', ts=4805000,
        data=[{'T': 4805000, 'p': '100', 'v': '10', 'S': 'Buy'}]))
    if production:
        from test_strategies import aggressive_buy_flow
        flow = aggressive_buy_flow()
        await send(MarketMessage(topic='publicTrade.AAA', ts=4805000,
            data=[{'T': t.ts_ms - 25200000, 'p': str(t.price), 'v': str(t.size), 'S': t.side} for t in flow]))
    live.set_running(True)
    assert live.running
    if capture:
        capture.started = True
        monitor = asyncio.create_task(capture.monitor())
    if file_capture:
        live.public_state()
    if production:
        for update, wall, bid in ((2, 4809, 100.30), (3, 4813, 100.32), (4, 4817, 100.34), (5, 4821, 100.36)):
            clock.set_observation(wall_seconds=wall, mono_ns=(wall-4795)*10**9)
            for depth in (50, 1000):
                await send(MarketMessage(topic=f'orderbook.{depth}.AAA', type='snapshot', ts=wall*1000,
                    data={'u': update, 'seq': update, 'b': [[str(bid), '1000']], 'a': [[str(bid+.01), '1000']]}))
            await send(MarketMessage(topic='publicTrade.AAA', ts=wall*1000,
                data=[{'T': (wall-4)*1000+i*150, 'p': str(bid+.01), 'v': str(8*(update-1)**2), 'S': 'Buy'} for i in range(24)]))
            await live._evaluate(live.sessions['AAA'])
    await live._evaluate(live.sessions['AAA'])
    live._arbitrate_once()
    if 'AAA' not in live.broker.positions:
        diagnostics = [(e['event'], e['payload'].get('reason'), e['payload'].get('blockers')) for e in live.events
                       if e['event'] not in ('decision', 'symbol_activated', 'scanner_update', 'clock_sync')]
        diagnostics.extend((d.action.value, d.reasons) for d in live.sessions['AAA'].decisions.values())
        await live.close()
        raise AssertionError(json.dumps(diagnostics, default=str))
    if exit_kind == 'stop':
        clock.set_observation(wall_seconds=4806, mono_ns=11*10**9)
        await send(MarketMessage(topic='orderbook.50.AAA', type='snapshot', ts=4806000,
            data={'u': 2, 'seq': 2, 'b': [['99', '1000']], 'a': [['99.02', '1000']]}))
        await send(MarketMessage(topic='publicTrade.AAA', ts=4806000,
            data=[{'T': 4806000, 'p': '99', 'v': '100', 'S': 'Sell'}]))
        assert not live.broker.positions
    if file_capture:
        live.public_state()
    if exit_kind == 'duration_elapsed':
        clock.set_observation(wall_seconds=4866, mono_ns=71*10**9)
        timer_wake.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not live.running:
                break
        assert not live.running
        assert live._last_run_summary['reason'] == 'duration_elapsed'
    if exit_kind == 'bot_stop':
        live.set_running(False)
    if capture:
        await asyncio.wait_for(monitor, timeout=5)
    else:
        await live.close()
    return live, prefix, rows


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_kind', ['shutdown', 'stop'])
async def test_cold_service_opens_and_closes_real_paper_position(tmp_path, monkeypatch, exit_kind):
    live, prefix, rows = await fixture(tmp_path, monkeypatch, exit_kind)
    replay = restore_cold_engine(prefix)
    install_signal(replay)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    async def forbidden(*args, **kwargs): raise AssertionError('offline IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'create_task', 'gather'):
                patch.setattr(asyncio, name, forbidden)
            result = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert result['serviceLifecycleMatched'] and result['outputsMatch']
        assert not result['parityReady']
        assert replay.broker.closed_trades == live.broker.closed_trades
        assert len(replay.broker.closed_trades) == 1
        trade = replay.broker.closed_trades[-1]
        assert trade['fees'] > 0 and trade['reason'] == exit_kind
        assert replay.broker.balance == live.broker.balance
        assert replay.broker.balance == pytest.approx(replay.config.start_balance + trade['netPnl'])
        assert not replay.broker.positions and not replay.broker.pending_entries
        assert sum(e['event'] == 'trade_opened' for e in outputs) == 1
        structural = write_report(tmp_path, [dict(event='replay_input', payload=r) for r in prefix + inputs])
        assert structural['structuralStatus'] == 'checks_passed', structural
    finally:
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['fees', 'netPnl', 'reason'])
async def test_portfolio_output_comparison_does_not_hide_financial_changes(tmp_path, monkeypatch, field):
    live, prefix, rows = await fixture(tmp_path, monkeypatch)
    replay = restore_cold_engine(prefix)
    install_signal(replay)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = deepcopy([r for r in rows if r['event'] != 'replay_input'])
    trade = next(r['payload'] for r in outputs if r['event'] == 'trade_closed')
    trade[field] = 'changed_reason' if field == 'reason' else trade[field] + .01
    try:
        with pytest.raises(SegmentMismatch, match='output events'):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await replay.close()


@pytest.mark.asyncio
async def test_production_breakout_signal_through_cold_replay(tmp_path, monkeypatch):
    live, prefix, rows = await fixture(tmp_path, monkeypatch, production=True)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    assert 'evaluate' not in live.strategies['level_breakout'].__dict__
    assert 'evaluate' not in replay.strategies['level_breakout'].__dict__
    async def forbidden(*args, **kwargs): raise AssertionError('offline IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'create_task', 'gather'):
                patch.setattr(asyncio, name, forbidden)
            result = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert result['outputsMatch'] and result['serviceLifecycleMatched']
        assert not result['parityReady']
        assert replay.broker.closed_trades == live.broker.closed_trades
        assert len(replay.broker.closed_trades) == 1
        assert replay.broker.closed_trades[0]['strategy'] == 'level_breakout'
        assert replay.broker.closed_trades[0]['fees'] > 0
        assert replay.broker.balance == live.broker.balance
        assert not replay.broker.positions and not replay.broker.pending_entries
        structural = write_report(tmp_path, [dict(event='replay_input', payload=r) for r in prefix + inputs])
        assert structural['structuralStatus'] == 'checks_passed', structural
    finally:
        await replay.close()

