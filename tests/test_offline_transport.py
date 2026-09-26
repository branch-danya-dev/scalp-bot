import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage, OrderBookSequenceError
from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import rehash


async def fixture(tmp_path, monkeypatch, restart=False, gap=False):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10*10**9)
    live = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=True, event_driven_evaluation_enabled=False), clock=clock, capture_inputs=True)
    prefix = [json.loads(x)['payload'] for x in live.recorder.path.read_text().splitlines()
              if json.loads(x)['event'] == 'replay_input']
    rows = []
    def record(event, symbol, payload): rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    live._apply_bootstrap_result('AAA', (None, None, [], [], [], []))
    async def stream(url, symbol, callback, stop, **kwargs):
        notify = kwargs['on_transport']
        fast, deep = 'orderbook.50.AAA', 'orderbook.1000.AAA'
        def event(topic, phase, attempt=1):
            notify(dict(phase=phase, attempt=attempt, topics=[topic],
                        errorType=('OrderBookSequenceError' if gap else 'ConnectionError') if phase == 'fault' else None, discarded=0))
        async def market(topic, kind, update, seq, bid):
            await callback(MarketMessage(topic=topic, type=kind, ts=1000000,
                receipt_mono_ns=10*10**9, data={'u': update, 'seq': seq,
                'b': [[str(bid), '5']], 'a': [['101', '6']] if kind == 'snapshot' else []}))
        for topic in (fast, deep):
            event(topic, 'connecting'); event(topic, 'subscription_sent')
        await market(fast, 'snapshot', 1, 1, 99)
        await market(deep, 'snapshot', 1, 2, 99)
        if gap:
            with pytest.raises(OrderBookSequenceError):
                await market(fast, 'delta', 4, 3, 99)
            event(fast, 'fault')
        else:
            event(fast, 'fault')
            await market(fast, 'delta', 2, 3, 99)  # Queued before disconnect; drained later.
        event(fast, 'drained')
        event(fast, 'connecting', 2); event(fast, 'subscription_sent', 2)
        await market(fast, 'snapshot', 1, 4, 98)
        event(fast, 'drained', 2); event(deep, 'drained')
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', stream)
    await live._symbol_worker('AAA', asyncio.Event())
    if restart:
        await live._symbol_worker('AAA', asyncio.Event())
    return live, prefix, [r['payload'] for r in rows if r['event'] == 'replay_input'], [r for r in rows if r['event'] != 'replay_input']


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_transport_reconnect_preserves_books_and_new_worker_resets_sequencer(tmp_path, monkeypatch, restart):
    live, prefix, inputs, outputs = await fixture(tmp_path, monkeypatch, restart)
    replay = restore_cold_engine(prefix)
    async def forbidden(*args, **kwargs): raise AssertionError('network access in replay')
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', forbidden)
    try:
        driver = OfflineScheduledReplay(replay)
        report = await driver.apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
        assert replay.sessions['AAA'].orderbook == live.sessions['AAA'].orderbook
        assert replay.sessions['AAA'].deep_orderbook == live.sessions['AAA'].deep_orderbook
        assert replay.sessions['AAA'].fast_receipt_mono == live.sessions['AAA'].fast_receipt_mono
        assert driver.transport.last_id == (2 if restart else 1)
        assert all(phase == 'drained' for w in driver.transport.workers.values() for _, phase in w['channels'].values())
    finally:
        await live.close(); await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['book', 'attempt', 'worker', 'after_drain'])
async def test_transport_rejects_rehashed_semantic_faults(tmp_path, monkeypatch, fault):
    live, prefix, inputs, outputs = await fixture(tmp_path, monkeypatch)
    replay = restore_cold_engine(prefix)
    row = next(r for r in inputs if r['kind'] == 'transport' and r['body']['phase'] == 'fault')
    if fault == 'book': row['body']['fastState']['bids'][0][1] += 1
    elif fault == 'attempt': row['body']['attempt'] += 1
    elif fault == 'worker': row['body']['workerId'] += 1
    else: row['body']['phase'] = 'drained'
    rehash(inputs)
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close(); await replay.close()


@pytest.mark.asyncio
async def test_transport_only_windows_continue_same_worker_state(tmp_path, monkeypatch):
    live, prefix, inputs, outputs = await fixture(tmp_path, monkeypatch)
    replay = restore_cold_engine(prefix)
    driver = OfflineScheduledReplay(replay)
    first = next(i for i, row in enumerate(inputs) if row['kind'] == 'transport')
    resume = next(i for i, row in enumerate(inputs) if row['kind'] == 'transport'
                  and row['body']['phase'] == 'connecting' and row['body']['attempt'] == 2)
    try:
        events = []
        for window in (inputs[:first], inputs[first:resume], inputs[resume:]):
            events.extend((await driver.apply(window))['events'])
        assert events == outputs
        assert replay.sessions['AAA'].orderbook == live.sessions['AAA'].orderbook
    finally:
        await live.close(); await replay.close()
