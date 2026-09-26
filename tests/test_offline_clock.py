from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import BybitError
from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import OfflineSegmentReplay, SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import segments, rehash


async def capture_clock(tmp_path):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10 * 10**9)
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=True, event_driven_evaluation_enabled=False),
        clock=clock, capture_inputs=True)
    prefix = [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
              if json.loads(line)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    live._apply_bootstrap_result('AAA', (None, None, [], [], [], []))
    sample = dict(server_ms=1000000, sent_mono=9.99, received_mono=10, received_wall_ms=1000000)
    async def response():
        return deepcopy(sample)
    live.rest.clock_sample = response
    assert await live._sync_clock_once()
    live.public_state('MISSING')  # Original selection and fallback must both replay.
    clock.set_observation(wall_seconds=1001, mono_ns=11 * 10**9)
    sample.update(server_ms=1001000, sent_mono=10, received_mono=11, received_wall_ms=1001000)
    assert not await live._sync_clock_once()  # Excessive RTT keeps previous bounded anchor.
    async def failure():
        raise BybitError('private transport details must not be copied')
    live.rest.clock_sample = failure
    assert not await live._sync_clock_once()
    live.market_health()
    assert live.sessions['AAA'].clock_reading['valid']
    clock.set_observation(wall_seconds=1062, mono_ns=72 * 10**9)
    live.public_state('AAA')
    assert live.sessions['AAA'].clock_reading['reason'] == 'synchronization_expired'
    live._arbitrate_once()
    sample.update(server_ms=1062000, sent_mono=71.99, received_mono=72, received_wall_ms=1062000)
    live.rest.clock_sample = response
    assert await live._sync_clock_once()
    live.public_state(None)
    assert live.sessions['AAA'].clock_reading['valid']
    assert live.sessions['AAA'].fast_receipt_mono is None  # Recovery does not freshen missing market data.
    return live, prefix, rows


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter', [OfflineSegmentReplay, OfflineScheduledReplay])
async def test_cold_clock_failure_expiry_recovery_and_ui_effects_match(tmp_path, adapter):
    live, prefix, rows = await capture_clock(tmp_path)
    replay = restore_cold_engine(prefix)
    try:
        driver = adapter(replay)
        for inputs, outputs in segments(rows):
            assert (await driver.apply(inputs, expected_events=outputs))['outputsMatch']
        assert vars(replay.market_clock) == vars(live.market_clock)
        assert replay.sessions['AAA'].clock_reading == live.sessions['AAA'].clock_reading
        assert replay.sessions['AAA'].clock_block_reason == live.sessions['AAA'].clock_block_reason
        assert list(replay.events) == list(live.events)
        assert 'private transport details' not in json.dumps(rows)
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
async def test_clock_and_ui_in_one_scheduled_window(tmp_path):
    live, prefix, rows = await capture_clock(tmp_path)
    replay = restore_cold_engine(prefix)
    try:
        inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
        outputs = [r for r in rows if r['event'] != 'replay_input']
        report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['sample', 'error_type', 'clock_method'])
async def test_rehashed_clock_changes_fail_output_or_call_comparison(tmp_path, fault):
    live, prefix, rows = await capture_clock(tmp_path)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    if fault == 'sample':
        next(r for r in inputs if r['kind'] == 'clock_sample')['body']['server_ms'] += 100
    elif fault == 'error_type':
        next(r for r in inputs if r['kind'] == 'clock_error')['body']['errorType'] = 'OtherError'
    else:
        next(r for r in inputs if r['kind'] == 'clock_read')['body']['method'] = 'monotonic'
    rehash(inputs)
    try:
        with pytest.raises(RuntimeError):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close()
        await replay.close()
