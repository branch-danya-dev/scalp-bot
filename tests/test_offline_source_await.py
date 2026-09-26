import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import Candidate, Candle
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_input_journal import capture, write_report
from test_offline_segment import rehash


async def fixture(tmp_path, periodic=False, cancel=None, failure=None):
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        event_driven_evaluation_enabled=False, working_symbols=2, max_active_symbols=2),
        clock=ReplayRuntimeClock(wall_seconds=1000, mono_ns=10**10), capture_inputs=True)
    prefix = [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
              if json.loads(line)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    live._launch_symbol_worker = lambda symbol: None
    live._apply_bootstrap_result('AAA', (None, None, [], [], [], []))
    scan_started, scan_release = asyncio.Event(), asyncio.Event()
    boot_started, boot_release = asyncio.Event(), asyncio.Event()
    done = asyncio.Event()
    async def ranking():
        scan_started.set()
        await scan_release.wait()
        if failure == 'scanner':
            raise ConnectionError('fixture scanner unavailable')
        return [Candidate('AAA', 2e8, .02, 100, activity_rank=1),
                Candidate('BBB', 1e8, .01, 100, activity_rank=2)]
    async def instrument(symbol):
        boot_started.set()
        await boot_release.wait()
        if failure == 'bootstrap':
            raise ValueError('fixture invalid instrument')
        return None
    async def fees(symbol): return None
    async def candles(*args): return [Candle(900000, 100, 101, 99, 100, 10, 1000)]
    live.rest.active_candidates = ranking
    live.rest.instrument_info = instrument
    live.rest.fee_schedule = fees
    live.rest.klines = candles
    waits = 0
    async def sleep(delay):
        nonlocal waits
        waits += 1
        if waits > 1:
            done.set()
            await asyncio.Event().wait()
    live._periodic_sleep = sleep
    task = asyncio.create_task(live._scanner_loop() if periodic else live._scan_once())
    handler = live._market_handler('AAA')[0]
    async def market(update):
        await handler(MarketMessage(topic='orderbook.50.AAA', type='snapshot', ts=1000000,
            receipt_mono_ns=10**10, data={'u': update, 'seq': update,
            'b': [[str(98 + update), '5']], 'a': [['102', '6']]}))
        live.public_state(selected_symbol='AAA')
    await asyncio.wait_for(scan_started.wait(), timeout=3)
    await market(1)
    if failure == 'scanner':
        scan_release.set()
        await asyncio.wait_for(done.wait(), timeout=3)
    elif cancel != 'scanner':
        scan_release.set()
        await asyncio.wait_for(boot_started.wait(), timeout=3)
        assert 'BBB' not in live.sessions
        await market(2)
        if cancel != 'bootstrap':
            boot_release.set()
            if periodic:
                await asyncio.wait_for(done.wait(), timeout=3)
            else:
                await asyncio.wait_for(task, timeout=3)
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return live, prefix, rows


@pytest.mark.asyncio
@pytest.mark.parametrize('periodic', [False, True])
@pytest.mark.parametrize('cancel', [None, 'scanner', 'bootstrap'])
async def test_scanner_and_bootstrap_allow_market_ui_and_cancel(tmp_path, monkeypatch, periodic, cancel):
    live, prefix, rows = await fixture(tmp_path, periodic, cancel)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    async def forbidden(*args, **kwargs): raise AssertionError('offline IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'create_task', 'gather'):
                patch.setattr(asyncio, name, forbidden)
            result = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert result['outputsMatch'] and not result['parityReady']
        assert replay.candidates == live.candidates
        assert list(replay.sessions) == list(live.sessions)
        assert ('BBB' in replay.sessions) == (cancel is None)
        assert replay.sessions['AAA'].orderbook == live.sessions['AAA'].orderbook
        assert replay._source_request_serial == live._source_request_serial
        assert list(replay.events) == list(live.events)
        if cancel is None:
            assert replay.sessions['BBB'].candles == live.sessions['BBB'].candles
    finally:
        await live.close()
        await replay.close()
    report = write_report(tmp_path, [dict(event='replay_input', payload=r) for r in prefix] + rows)
    assert report['structuralStatus'] == 'checks_passed', report


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['identity', 'symbol', 'missing', 'raised'])
async def test_rehashed_source_resume_is_rejected(tmp_path, fault):
    live, prefix, rows = await fixture(tmp_path)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    target = next(r for r in inputs if r['kind'] == 'source_await'
                  and r['body']['source'] == 'bootstrap' and r['body']['phase'] == 'ready')
    if fault == 'identity': target['body']['id'] += 1
    elif fault == 'symbol': target['symbol'] = 'WRONG'
    elif fault == 'raised': target['body']['phase'] = 'raised'
    else:
        inputs.remove(target)
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


@pytest.mark.parametrize('fault,issue', [('id', 'unmatched_source_resume'),
    ('symbol', 'unmatched_source_resume'), ('duplicate', 'duplicate_source_wait'),
    ('missing', 'unfinished_source_waits')])
def test_validator_binds_source_wait_to_completion(tmp_path, fault, issue):
    journal, _, rows = capture()
    body = dict(id=1, source='bootstrap', phase='wait')
    journal.append('source_await', 'AAA', body)
    if fault == 'duplicate': journal.append('source_await', 'AAA', body)
    if fault != 'missing':
        journal.append('source_await', 'BBB' if fault == 'symbol' else 'AAA',
                       dict(body, phase='ready', id=2 if fault == 'id' else 1))
    journal.close()
    report = write_report(tmp_path, rows)
    assert report['structuralStatus'] == 'rejected' and issue in report['issues']
