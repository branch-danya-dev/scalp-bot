import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Candle
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import rehash


async def fixture(tmp_path, empty=False, fail_then_recover=False):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10*10**9)
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        confirmed_candle_stale_seconds=90, event_driven_evaluation_enabled=False),
        clock=clock, capture_inputs=True)
    prefix = [json.loads(x)['payload'] for x in live.recorder.path.read_text().splitlines()
              if json.loads(x)['event'] == 'replay_input']
    rows, calls = [], []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    if not empty:
        for symbol, start in [('AAA', 600000), ('BBB', 900000)]:
            live._apply_bootstrap_result(symbol, (None, None,
                [Candle(start, 100, 101, 99, 100, 10, 1000)], [], [], []))
    failing = fail_then_recover
    async def klines(symbol, interval, limit):
        calls.append((symbol, interval))
        if failing and symbol == 'AAA':
            raise ValueError('PRIVATE-REST-DETAIL')
        if symbol == 'AAA':
            await asyncio.sleep(0)  # Later completion must not reorder application.
        return [Candle(900000, 100, 102, 99, 101, 20, 2000, True)]
    live.rest.klines = klines
    queue, finished = asyncio.Queue(), asyncio.Event()
    waits = 0
    async def wait(delay):
        nonlocal waits
        waits += 1
        if waits >= 2:
            finished.set()
        await queue.get()
    live._periodic_sleep = wait
    task = asyncio.create_task(live._context_loop())
    await asyncio.sleep(0)
    queue.put_nowait(None)
    await asyncio.wait_for(finished.wait(), timeout=3)
    if fail_then_recover:
        assert live.sessions['AAA'].candles[-1].start_ms == 600000
        assert live.sessions['BBB'].context_5m
        failing = False
        finished.clear()
        queue.put_nowait(None)
        await asyncio.wait_for(finished.wait(), timeout=3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return live, prefix, rows, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('empty', [False, True])
async def test_context_loop_replays_freshness_branch_batch_order_and_cancel(tmp_path, monkeypatch, empty):
    live, prefix, rows, calls = await fixture(tmp_path, empty)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    async def forbidden(*args, **kwargs): raise AssertionError('unexpected async IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'create_task', 'gather'):
                patch.setattr(asyncio, name, forbidden)
            result = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert result['outputsMatch'] and not result['parityReady']
        assert list(replay.events) == list(live.events)
        for symbol in live.sessions:
            for attr in ('candles', 'context_5m', 'context_15m', 'context_1h', 'static_analysis_key'):
                assert getattr(replay.sessions[symbol], attr) == getattr(live.sessions[symbol], attr)
        if not empty:
            assert ('AAA', '1') in calls and ('BBB', '1') not in calls
            context = [r for r in inputs if r['kind'] == 'rest_context']
            assert [r['symbol'] for r in context] == ['AAA', 'BBB']
            assert context[1]['body']['candles'] is None
        else:
            assert calls == [] and not replay.sessions
        assert '_fetch_context_results' not in replay.__dict__
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['branch', 'order', 'missing'])
async def test_context_refuses_wrong_batch_even_with_valid_hashes(tmp_path, fault):
    live, prefix, rows, _ = await fixture(tmp_path)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    selected = [r for r in inputs if r['kind'] == 'rest_context']
    if fault == 'branch':
        selected[1]['body']['candles'] = []
    elif fault == 'order':
        selected[0]['symbol'], selected[1]['symbol'] = selected[1]['symbol'], selected[0]['symbol']
    else:
        selected[1]['kind'] = 'callback'
        selected[1]['body'] = {'name': 'arbiter'}
        selected[1]['symbol'] = None
    rehash(inputs)
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.input_journal is None and replay.replay_origin['state'] == 'failed'
    finally:
        await live.close()
        await replay.close()
