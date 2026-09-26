import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Candidate
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay, comparable_events
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import rehash


async def fixture(tmp_path, monkeypatch, exchange=True, immediate=False, trading=False, failure=None):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10*10**9)
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        working_symbols=1, exchange_clock_enabled=exchange, event_driven_evaluation_enabled=False),
        clock=clock, capture_inputs=True)
    prefix = [json.loads(x)['payload'] for x in engine.recorder.path.read_text().splitlines()
              if json.loads(x)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload): rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    engine.recorder.record = engine.input_journal.record = record
    engine.recorder.start_background_writer = lambda: None
    async def candidates():
        if failure == 'scanner': raise ConnectionError('fixture startup scanner unavailable')
        return [Candidate('AAA', 2e8, .02, 100)]
    async def metadata(symbol):
        if failure == 'bootstrap': raise ValueError('fixture startup invalid instrument')
        return None
    async def klines(*args): return []
    async def clock_sample():
        return dict(server_ms=1000000, sent_mono=9.99, received_mono=10, received_wall_ms=1000000)
    engine.rest.active_candidates = candidates
    engine.rest.instrument_info = engine.rest.fee_schedule = metadata
    engine.rest.klines = klines
    engine.rest.clock_sample = clock_sample
    async def wait(delay): await asyncio.Event().wait()
    engine._periodic_sleep = wait
    async def stream(url, symbol, callback, stop, **kwargs):
        def notify(phase):
            kwargs['on_transport'](dict(phase=phase, attempt=1,
                topics=['orderbook.50.AAA'], errorType=None, discarded=0))
        notify('connecting'); notify('subscription_sent')
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            notify('cancelled')
            raise
        finally:
            notify('drained')
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', stream)
    await engine.start()
    if trading:
        engine.start_block_reason = lambda: None
        engine.set_running(True)
    if not immediate:
        await asyncio.sleep(0)
    await engine.close()
    return engine, prefix, [r['payload'] for r in rows if r['event'] == 'replay_input'], [r for r in rows if r['event'] != 'replay_input']


@pytest.mark.asyncio
@pytest.mark.parametrize('exchange,immediate,trading', [(True, False, False), (False, False, False),
                                                       (True, True, False), (True, False, True)])
async def test_cold_service_start_and_close_replays_complete_fixture(tmp_path, monkeypatch, exchange, immediate, trading):
    live, prefix, inputs, outputs = await fixture(tmp_path, monkeypatch, exchange, immediate, trading)
    replay = restore_cold_engine(prefix)
    if trading:
        replay.start_block_reason = lambda: None
    async def forbidden(*args, **kwargs): raise AssertionError('unexpected live scheduling')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'gather', 'create_task'):
                patch.setattr(asyncio, name, forbidden)
            report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['serviceLifecycleMatched'] and report['outputsMatch']
        assert not report['parityReady']
        assert replay.replay_origin['state'] == 'closed'
        assert replay._stop.is_set() and not replay.running
        assert replay.candidates == live.candidates
        assert comparable_events(list(replay.events)) == comparable_events(list(live.events))
        assert vars(replay.market_clock) == vars(live.market_clock)
        with pytest.raises(SegmentMismatch, match='already closed'):
            await OfflineScheduledReplay(replay).apply(inputs)
    finally:
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['footer_missing', 'early_footer', 'start_missing', 'duplicate_start'])
async def test_service_rejects_incomplete_or_reordered_cleanup(tmp_path, monkeypatch, fault):
    live, prefix, inputs, outputs = await fixture(tmp_path, monkeypatch)
    replay = restore_cold_engine(prefix)
    if fault == 'footer_missing':
        inputs = inputs[:-1]
    elif fault == 'early_footer':
        close = next(i for i, r in enumerate(inputs) if r['kind'] == 'service' and r['body']['phase'] == 'close')
        inputs = inputs[:close+1] + inputs[-1:]
    elif fault == 'start_missing':
        inputs[0]['body']['phase'] = 'close'
    else:
        next(r for r in inputs if r['kind'] == 'service' and r['body']['phase'] == 'close')['body']['phase'] = 'start'
    for i, row in enumerate(inputs, 4): row['sequence'] = i
    rehash(inputs)
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
        assert '_launch_service_tasks' not in replay.__dict__
    finally:
        await replay.close()
