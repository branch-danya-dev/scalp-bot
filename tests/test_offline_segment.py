from copy import deepcopy

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import Candle
from scalp_bot.engine import TradingEngine
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.offline_segment import OfflineSegmentReplay, SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock, SystemRuntimeClock


def segments(rows):
    inputs, outputs, depth = [], [], 0
    result = []
    for row in rows:
        if row['event'] == 'replay_input':
            item = row['payload']
            if item['kind'] == 'scope' and item['body']['phase'] == 'begin':
                depth += 1
            if depth:
                inputs.append(item)
            if item['kind'] == 'scope' and item['body']['phase'] == 'end':
                depth -= 1
                if depth == 0:
                    result.append((inputs, outputs))
                    inputs, outputs = [], []
        elif depth:
            outputs.append(row)
    return result


def rehash(rows):
    for i, row in enumerate(rows):
        if i:
            row['previousHash'] = rows[i - 1]['hash']
        row['hash'] = fingerprint({k: v for k, v in row.items() if k != 'hash'})


async def captured(tmp_path):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    config = Settings(_env_file=None, session_dir=str(tmp_path / 'capture'),
                      event_driven_evaluation_enabled=False, exchange_clock_enabled=True)
    engine = TradingEngine(config, clock=clock, capture_inputs=True)
    rows = []
    original = engine.recorder.record
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
        original(event, symbol, payload)
    engine.recorder.record = record
    engine.input_journal.record = record
    engine._apply_bootstrap_result('AAA', (None, None,
        [Candle(900000, 100, 101, 99, 100, 10, 1000)], [], [], []))
    handler = engine._market_handler('AAA')[0]
    for i, message in enumerate([
        MarketMessage(topic=f'orderbook.{config.deep_orderbook_depth}.AAA', type='snapshot',
                      ts=1000000, data={'u': 1, 'seq': 1, 'b': [['99', '5']], 'a': [['101', '6']]}),
        MarketMessage(topic=f'orderbook.{config.fast_orderbook_depth}.AAA', type='snapshot',
                      ts=1000001, data={'u': 1, 'seq': 2, 'b': [['99', '5']], 'a': [['101', '6']]}),
        MarketMessage(topic=f'orderbook.{config.fast_orderbook_depth}.AAA', type='delta',
                      ts=1000002, data={'u': 2, 'seq': 3, 'b': [['99', '7']], 'a': []}),
        MarketMessage(topic='publicTrade.AAA', ts=1000003,
                      data=[{'T': 1000003, 'p': '100', 'v': '2', 'S': 'Buy'},
                            {'T': 1000004, 'p': '100.5', 'v': '3', 'S': 'Buy'}]),
    ]):
        clock.set_observation(wall_seconds=1001 + i, mono_ns=(11 + i) * 10**9)
        message.receipt_mono_ns = (11 + i) * 10**9
        message.processor_started_mono_ns = message.receipt_mono_ns
        await handler(message)
    engine._apply_context_result(engine.sessions['AAA'], (None, [], [], []))
    engine._arbitrate_once()
    batches = segments(rows)
    return engine, config, clock, batches


@pytest.mark.asyncio
async def test_shared_market_path_replays_nested_scopes_clocks_and_outputs(tmp_path, monkeypatch):
    live, config, clock, batches = await captured(tmp_path)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'replay')}), clock=clock)
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected network or OS clock')
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', forbidden)
    for name in ('time', 'monotonic', 'perf_counter_ns'):
        monkeypatch.setattr(SystemRuntimeClock, name, forbidden)
    driver = OfflineSegmentReplay(replay)
    try:
        assert len(batches) == 7
        for records, expected in batches:
            report = await driver.apply(records, expected_events=expected)
            assert report['events'] == expected
            assert report['outputsMatch'] is True
            assert report['inputsConsumed'] == len(records)
            assert report['parityReady'] is False
        assert replay.sessions['AAA'].market_snapshot() == live.sessions['AAA'].market_snapshot()
        assert len(replay.sessions['AAA'].trades) == 2
        assert replay.sessions['AAA'].orderbook.bids == live.sessions['AAA'].orderbook.bids
        assert replay.clock is clock and replay.sessions['AAA'].clock is clock
        with pytest.raises(SegmentMismatch, match='already consumed'):
            await driver.apply(batches[-1][0])
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
async def test_trade_stop_replays_execution_events_fees_and_balance(tmp_path):
    from test_engine_lifecycle import plan, book
    live, config, clock, batches = await captured(tmp_path)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'replay')}), clock=clock)
    driver = OfflineSegmentReplay(replay)
    try:
        for records, expected in batches:
            await driver.apply(records, expected_events=expected)
        # Explicit identical checkpoint; opening itself is outside this segment.
        for engine in (live, replay):
            engine.broker.open(plan('AAA'), book())
        rows = []
        original = live.recorder.record
        def record(event, symbol, payload):
            rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
            original(event, symbol, payload)
        live.recorder.record = live.input_journal.record = record
        await live._market_handler('AAA')[0](MarketMessage(topic='publicTrade.AAA',
            ts=1005000, receipt_mono_ns=14_000_000_000,
            data=[{'T': 1005000, 'p': '99', 'v': '100', 'S': 'Sell'}]))
        records, expected = segments(rows)[0]
        report = await driver.apply(records, expected_events=expected)
        assert report['outputsMatch']
        assert any(e['event'] == 'trade_closed' for e in expected)
        assert replay.broker.closed_trades == live.broker.closed_trades
        trade = replay.broker.closed_trades[-1]
        assert trade['reason'] == 'stop' and trade['fees'] > 0
        assert replay.broker.balance == live.broker.balance
        assert not replay.broker.positions
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
async def test_output_mismatch_poisoning(tmp_path):
    live, config, clock, batches = await captured(tmp_path)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'replay')}), clock=clock)
    driver = OfflineSegmentReplay(replay)
    try:
        with pytest.raises(SegmentMismatch, match='output events'):
            await driver.apply(batches[0][0], expected_events=[])
        assert driver.failed
        assert replay.clock is clock and replay.input_journal is None
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['hash', 'gap', 'scheduler', 'interleaving', 'clock', 'outcome'])
async def test_replay_rejects_corrupt_or_unsupported_segments(tmp_path, fault):
    live, config, clock, batches = await captured(tmp_path)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'replay')}), clock=clock)
    driver = OfflineSegmentReplay(replay)
    rows = deepcopy(batches[0][0])
    if fault == 'hash':
        rows[1]['hash'] = 'a' * 64
    elif fault == 'gap':
        rows[1]['sequence'] += 100
        rehash(rows)
    elif fault == 'scheduler':
        rows[1]['kind'] = 'scheduler'
        rehash(rows)
    elif fault == 'interleaving':
        rows[-1]['body'] = dict(phase='begin', id=999, parentId=None, name='evaluate')
        rehash(rows)
    elif fault == 'outcome':
        rows[-1]['body']['outcome'] = 'raised'
        rehash(rows)
    else:
        row = next(r for r in rows if r['kind'] == 'clock_read')
        row['body']['method'] = 'monotonic'
        rehash(rows)
    try:
        with pytest.raises(RuntimeError):
            await driver.apply(rows)
        assert replay.input_journal is None and replay.clock is clock
        if fault == 'clock':
            assert driver.failed
            with pytest.raises(SegmentMismatch, match='previous replay failure'):
                await driver.apply(batches[0][0])
        else:
            assert not replay.sessions  # Rejected before applying anything.
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
async def test_replay_never_silently_disables_configured_scheduler(tmp_path):
    live, config, clock, batches = await captured(tmp_path)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'replay'),
                                                    'event_driven_evaluation_enabled': True}), clock=clock)
    try:
        with pytest.raises(SegmentMismatch, match='scheduler disabled'):
            await OfflineSegmentReplay(replay).apply(batches[0][0])
        assert replay.config.event_driven_evaluation_enabled and not replay.sessions
    finally:
        await live.close()
        await replay.close()
