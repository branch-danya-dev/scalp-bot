import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_input_journal import write_report
from test_offline_segment import rehash


async def fixture(tmp_path, outcome='success', periodic=False):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10**10)
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=True, clock_max_sync_age_seconds=5,
        event_driven_evaluation_enabled=False), clock=clock, capture_inputs=True)
    prefix = [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
              if json.loads(line)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    live._apply_bootstrap_result('AAA', (None, None, [], [], [], []))
    async def initial():
        return dict(server_ms=1000000, sent_mono=9.99, received_mono=10, received_wall_ms=1000000)
    live.rest.clock_sample = initial
    assert await live._sync_clock_once()
    started, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def response():
        started.set()
        await release.wait()
        if outcome == 'error':
            raise ConnectionError('PRIVATE-CLOCK-DETAIL')
        return dict(server_ms=1007000, sent_mono=10 if outcome == 'rejected' else 16.99,
                    received_mono=17, received_wall_ms=1007000)
    live.rest.clock_sample = response
    waits = 0
    async def sleep(delay):
        nonlocal waits
        waits += 1
        if waits > 1:
            done.set()
            await asyncio.Event().wait()
    live._periodic_sleep = sleep
    task = asyncio.create_task(live._clock_loop() if periodic else live._sync_clock_once())
    await asyncio.wait_for(started.wait(), timeout=3)
    clock.set_observation(wall_seconds=1007, mono_ns=17*10**9)
    handler = live._market_handler('AAA')[0]
    await handler(MarketMessage(topic='orderbook.50.AAA', type='snapshot', ts=1007000,
        receipt_mono_ns=17*10**9, data={'u': 1, 'seq': 1, 'b': [['99', '5']], 'a': [['101', '6']]}))
    live.public_state('AAA')
    live._arbitrate_once()
    assert live.sessions['AAA'].clock_reading['reason'] == 'synchronization_expired'
    if outcome != 'cancel':
        release.set()
        if periodic:
            await asyncio.wait_for(done.wait(), timeout=3)
        else:
            assert await asyncio.wait_for(task, timeout=3) == (outcome == 'success')
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    live.public_state('AAA')
    assert live.sessions['AAA'].clock_reading['valid'] == (outcome == 'success')
    return live, prefix, rows


@pytest.mark.asyncio
@pytest.mark.parametrize('periodic', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'rejected', 'error', 'cancel'])
async def test_clock_await_expiry_recovery_error_and_cancel(tmp_path, monkeypatch, periodic, outcome):
    live, prefix, rows = await fixture(tmp_path, outcome, periodic)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    assert 'PRIVATE-CLOCK-DETAIL' not in json.dumps(rows)
    async def forbidden(*args, **kwargs): raise AssertionError('offline IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'create_task', 'gather'):
                patch.setattr(asyncio, name, forbidden)
            result = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert result['outputsMatch'] and not result['parityReady']
        assert vars(replay.market_clock) == vars(live.market_clock)
        for attr in ('clock_reading', 'clock_block_reason', 'orderbook'):
            assert getattr(replay.sessions['AAA'], attr) == getattr(live.sessions['AAA'], attr)
        assert list(replay.events) == list(live.events)
    finally:
        await live.close()
        await replay.close()
    report = write_report(tmp_path, [dict(event='replay_input', payload=r) for r in prefix] + rows)
    assert report['structuralStatus'] == 'checks_passed', report


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['source', 'id', 'missing_result'])
async def test_clock_await_rejects_rehashed_mismatches(tmp_path, fault):
    live, prefix, rows = await fixture(tmp_path)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    target = [r for r in inputs if r['kind'] == 'source_await' and r['body']['phase'] == 'ready'][-1]
    if fault == 'source': target['body']['source'] = 'scanner'
    elif fault == 'id': target['body']['id'] += 1
    else:
        inputs.remove([r for r in inputs if r['kind'] == 'clock_sample'][-1])
        for index, row in enumerate(inputs, start=inputs[0]['sequence']):
            row['sequence'] = index
    rehash(inputs)
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineScheduledReplay(replay).apply(inputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close()
        await replay.close()
