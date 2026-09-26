import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay, comparable_events
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from scalp_bot.manifest_validation import fingerprint
from test_offline_segment import rehash
from test_manifest_validation import rehash as rehash_manifest


async def fixture(tmp_path, mode):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10*10**9)
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        paper_run_duration_seconds=5, event_driven_evaluation_enabled=False), clock=clock, capture_inputs=True)
    prefix = [json.loads(x)['payload'] for x in live.recorder.path.read_text().splitlines()
              if json.loads(x)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload): rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    live.start_block_reason = lambda: None  # Isolate control/timer behavior from market readiness.
    queue = asyncio.Queue()
    async def wait(delay): await queue.get()
    live._periodic_sleep = wait
    live.toggle_strategy('level_breakout', False)
    live.set_running(True)
    task = live._paper_timer_task
    if mode != 'prestart_cancel':
        await asyncio.sleep(0)
    live.toggle_strategy('level_breakout', True)
    clock.set_observation(wall_seconds=1005, mono_ns=15*10**9)
    if mode == 'expiry':
        queue.put_nowait(None)
    else:
        live.set_running(False)
    await asyncio.gather(task, return_exceptions=True)
    return live, prefix, [r['payload'] for r in rows if r['event'] == 'replay_input'], [r for r in rows if r['event'] != 'replay_input']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['expiry', 'manual', 'prestart_cancel'])
async def test_controls_run_manifest_timer_and_stop_match(tmp_path, monkeypatch, mode):
    live, prefix, inputs, outputs = await fixture(tmp_path, mode)
    replay = restore_cold_engine(prefix)
    replay.start_block_reason = lambda: None
    async def forbidden(*args, **kwargs): raise AssertionError('live task/wait in offline control')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, 'sleep', forbidden)
            patch.setattr(asyncio, 'create_task', forbidden)
            report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
        assert not replay.running
        assert replay.strategy_enabled == live.strategy_enabled
        assert replay._run_manifest == live._run_manifest
        a, b = deepcopy(replay._last_run_summary), deepcopy(live._last_run_summary)
        for key in ('latencyMetrics', 'recorderHealth'):
            a.pop(key); b.pop(key)
        assert a == b
        assert a['reason'] == ('duration_elapsed' if mode == 'expiry' else 'bot_stop')
        assert replay.replay_origin['strategiesSha256'] == fingerprint(replay.strategy_enabled)
        assert '_launch_run_timer' not in replay.__dict__
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['pnl', 'reason', 'manifest'])
async def test_controls_refuse_economic_or_manifest_mismatch(tmp_path, fault):
    live, prefix, inputs, outputs = await fixture(tmp_path, 'expiry')
    replay = restore_cold_engine(prefix)
    replay.start_block_reason = lambda: None
    if fault == 'manifest':
        manifest = next(r for r in inputs if r['kind'] == 'manifest')['body']['manifest']
        manifest['strategies']['enabled'] = []
        manifest['strategies']['tradeable'] = []
        manifest['strategies']['evidenceOnly'] = []
        rehash_manifest(manifest)
        rehash(inputs)
    else:
        summary = next(r for r in outputs if r['event'] == 'run_summary')['payload']
        summary['realizedPnl' if fault == 'pnl' else 'reason'] = 123 if fault == 'pnl' else 'invented'
    try:
        with pytest.raises(RuntimeError):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close()
        await replay.close()


def test_only_run_summary_operational_fields_are_excluded():
    events = [dict(event='run_summary', symbol=None, payload=dict(realizedPnl=1, latencyMetrics=2, recorderHealth=3)),
              dict(event='trade_closed', symbol='AAA', payload=dict(realizedPnl=1, recorderHealth=3))]
    result = comparable_events(events)
    assert result[0]['payload'] == {'realizedPnl': 1}
    assert result[1] == events[1]
    assert events[0]['payload']['recorderHealth'] == 3
