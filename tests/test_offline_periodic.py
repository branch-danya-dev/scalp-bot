import asyncio
from copy import deepcopy

import pytest

from scalp_bot.bybit import BybitError
from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import rehash


async def fixture(tmp_path):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10 * 10**9)
    config = Settings(_env_file=None, session_dir=str(tmp_path / 'live'),
        exchange_clock_enabled=True, event_driven_evaluation_enabled=False, clock_sync_interval_seconds=4)
    live = TradingEngine(config, clock=clock, capture_inputs=True)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'offline')}), clock=clock)
    live.running = replay.running = True  # Explicit prepared state; operator Start is outside this test.
    queues = {'clock': asyncio.Queue(), 'arbiter': asyncio.Queue()}
    async def controlled_sleep(delay):
        await queues[asyncio.current_task().get_name()].get()
    live._periodic_sleep = controlled_sleep
    fail = False
    async def response():
        if fail:
            raise BybitError('fixture failure')
        return dict(server_ms=clock.time()*1000, sent_mono=clock.monotonic()-.01,
                    received_mono=clock.monotonic(), received_wall_ms=clock.time()*1000)
    live.rest.clock_sample = response
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    tasks = [asyncio.create_task(live._clock_loop(), name='clock'),
             asyncio.create_task(live._arbiter_loop(), name='arbiter')]
    await asyncio.sleep(0)
    async def wake(source, mono):
        clock.set_observation(wall_seconds=990 + mono, mono_ns=int(mono*10**9))
        queues[source].put_nowait(None)
        await asyncio.sleep(0)
    await wake('clock', 12)
    live.public_state(None)
    await wake('arbiter', 12.25)
    fail = True
    await wake('clock', 16)
    fail = False
    await wake('clock', 18)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    return live, replay, inputs, outputs


@pytest.mark.asyncio
async def test_periodic_clock_retry_arbiter_ui_and_cancellation_without_real_waits(tmp_path, monkeypatch):
    live, replay, inputs, outputs = await fixture(tmp_path)
    async def forbidden(*args, **kwargs):
        raise AssertionError('unexpected live wait/task')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, 'sleep', forbidden)
            patch.setattr(asyncio, 'create_task', forbidden)
            report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
        assert vars(replay.market_clock) == vars(live.market_clock)
        assert list(replay.events) == list(live.events)
        waits = [r['body']['delay'] for r in inputs if r['kind'] == 'dispatch'
                 and r['body']['source'] == 'clock' and r['body']['phase'] == 'wait']
        assert waits == [2, 4, 2, 4]
        assert any(r['kind'] == 'scope' and r['body'].get('name') == 'arbiter' for r in inputs)
        assert '_periodic_sleep' not in replay.__dict__ and '_sync_clock_once' not in replay.__dict__
    finally:
        live.running = replay.running = False
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['id', 'delay', 'source', 'unfinished'])
async def test_periodic_dispatch_rejects_semantic_mismatches(tmp_path, fault):
    live, replay, inputs, outputs = await fixture(tmp_path)
    wake = next(r for r in inputs if r['kind'] == 'dispatch' and r['body']['phase'] == 'wake')
    if fault == 'id':
        wake['body']['id'] += 1
    elif fault == 'delay':
        wake['body']['delay'] += 1
    elif fault == 'source':
        wake['body']['source'] = 'context'
    else:
        end = next(i for i, r in enumerate(inputs) if r['kind'] == 'dispatch' and r['body']['phase'] == 'cancelled')
        inputs = inputs[:end]
    rehash(inputs)
    driver = OfflineScheduledReplay(replay)
    try:
        with pytest.raises(SegmentMismatch):
            await driver.apply(inputs, expected_events=outputs)
        assert driver.failed and replay.input_journal is None
        assert '_periodic_sleep' not in replay.__dict__
    finally:
        live.running = replay.running = False
        await live.close()
        await replay.close()
