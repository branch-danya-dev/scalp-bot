import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import Candle
from scalp_bot.engine import TradingEngine
from scalp_bot.input_journal import valid_body
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import rehash
from test_input_journal import capture, write_report


async def capture_wait(tmp_path, cancel=False):
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        confirmed_candle_stale_seconds=90, event_driven_evaluation_enabled=False),
        clock=ReplayRuntimeClock(wall_seconds=1000, mono_ns=10**10), capture_inputs=True)
    prefix = [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
              if json.loads(line)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    live._apply_bootstrap_result('AAA', (None, None,
        [Candle(600000, 100, 101, 99, 100, 10, 1000)], [], [], []))
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    queue = asyncio.Queue()
    async def klines(*args):
        started.set()
        await release.wait()
        return [Candle(900000, 100, 102, 99, 101, 20, 2000, True)]
    live.rest.klines = klines
    waits = 0
    async def periodic_sleep(delay):
        nonlocal waits
        waits += 1
        if waits == 2:
            finished.set()
        await queue.get()
    live._periodic_sleep = periodic_sleep
    task = asyncio.create_task(live._context_loop())
    queue.put_nowait(None)
    await asyncio.wait_for(started.wait(), timeout=3)
    assert live.sessions['AAA'].candles[-1].start_ms == 600000
    handler, _, _ = live._market_handler('AAA')
    await handler(MarketMessage(topic='orderbook.50.AAA', type='snapshot', ts=1000000,
        receipt_mono_ns=10**10, data={'u': 1, 'seq': 1, 'b': [['99', '5']], 'a': [['101', '6']]}))
    live.public_state(selected_symbol='AAA')
    if not cancel:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return live, prefix, rows


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_market_and_ui_during_rest_wait_replay_without_io(tmp_path, monkeypatch, cancel):
    live, prefix, rows = await capture_wait(tmp_path, cancel)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    phases = [r['body']['phase'] for r in inputs if r['kind'] == 'context_await']
    assert phases == ['wait', 'request', 'cancelled' if cancel else 'ready']
    request = next(i for i, r in enumerate(inputs) if r['kind'] == 'context_await' and r['body']['phase'] == 'request')
    finish = next(i for i, r in enumerate(inputs) if r['kind'] == 'context_await' and r['body']['phase'] in ('ready', 'cancelled'))
    assert any(r['kind'] == 'market_message' for r in inputs[request:finish])
    async def forbidden(*args, **kwargs):
        raise AssertionError('unexpected offline IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'gather', 'create_task'):
                patch.setattr(asyncio, name, forbidden)
            report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
        assert replay.sessions['AAA'].orderbook == live.sessions['AAA'].orderbook
        assert replay.sessions['AAA'].candles == live.sessions['AAA'].candles
        assert replay.sessions['AAA'].candles[-1].start_ms == (600000 if cancel else 900000)
        assert replay._context_batch_serial == live._context_batch_serial == 1
        assert list(replay.events) == list(live.events)
    finally:
        await live.close()
        await replay.close()
    all_rows = [dict(event='replay_input', payload=r) for r in prefix] + rows
    validation = write_report(tmp_path, all_rows)
    assert validation['structuralStatus'] == 'checks_passed', validation


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['identity', 'symbol', 'premature', 'missing_finish'])
async def test_rehashed_context_boundaries_fail_closed(tmp_path, fault):
    live, prefix, rows = await capture_wait(tmp_path)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    request = next(r for r in inputs if r['kind'] == 'context_await' and r['body']['phase'] == 'request')
    if fault == 'identity':
        request['body']['id'] += 1
    elif fault == 'symbol':
        request['symbol'] = 'BBB'
    elif fault == 'premature':
        request['body']['phase'] = 'ready'
        request['symbol'] = None
    else:
        inputs = [r for r in inputs if not (r['kind'] == 'context_await' and r['body']['phase'] == 'ready')]
        for index, row in enumerate(inputs, start=inputs[0]['sequence']):
            row['sequence'] = index
    rehash(inputs)
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close()
        await replay.close()


@pytest.mark.parametrize('body,symbol', [
    ({'id': True, 'phase': 'request'}, 'AAA'),
    ({'id': 1, 'phase': 'request'}, None),
    ({'id': 1, 'phase': 'ready'}, 'AAA'),
    ({'id': 1, 'phase': 'wait', 'symbols': ['AAA', 'AAA']}, None),
    ({'id': 1, 'phase': 'wait', 'symbols': []}, None),
])
def test_context_boundary_schema_rejects_ambiguous_inputs(body, symbol):
    assert not valid_body('context_await', symbol, body)


@pytest.mark.parametrize('fault,issue', [
    ('duplicate', 'invalid_context_wait'),
    ('unknown', 'unmatched_context_await'),
    ('wrong_symbol', 'invalid_context_request'),
    ('early', 'premature_context_ready'),
    ('unfinished', 'unfinished_context_await'),
])
def test_validator_checks_context_await_lifecycle(tmp_path, fault, issue):
    journal, _, rows = capture()
    journal.append('context_await', None, {'id': 1, 'phase': 'wait', 'symbols': ['AAA']})
    if fault == 'duplicate':
        journal.append('context_await', None, {'id': 1, 'phase': 'wait', 'symbols': ['AAA']})
    if fault not in ('early', 'unfinished'):
        journal.append('context_await', 'BBB' if fault == 'wrong_symbol' else 'AAA',
            {'id': 2 if fault == 'unknown' else 1, 'phase': 'request'})
    if fault != 'unfinished':
        journal.append('context_await', None, {'id': 1, 'phase': 'ready'})
    journal.close()
    result = write_report(tmp_path, rows)
    assert result['structuralStatus'] == 'rejected'
    assert issue in result['issues']
