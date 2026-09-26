import asyncio
from copy import deepcopy

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import Action, StrategyDecision
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.runtime_clock import ReplayRuntimeClock


async def fixture(tmp_path, cancel, symbols=('AAA',)):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    config = Settings(_env_file=None, session_dir=str(tmp_path / 'live'),
        event_driven_evaluation_enabled=True, event_evaluation_min_interval_seconds=.02,
        exchange_clock_enabled=True)
    live = TradingEngine(config, clock=clock, capture_inputs=True)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'offline')}), clock=clock)
    for engine in (live, replay):
        engine._session_engaged = lambda session: True
        for symbol in symbols:
            engine._apply_bootstrap_result(symbol, (None, None, [], [], [], []))
            session = engine.sessions[symbol]
            session.last_event_eval_at = 10
            session.decisions['level_breakout'] = StrategyDecision(strategy='level_breakout',
                action=Action.WAIT, confidence=.8, reasons=['fixture'], watched_level=100,
                details={'state': 'armed'})
    rows = []
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
    live.recorder.record = live.input_journal.record = record
    handlers = {symbol: live._market_handler(symbol)[0] for symbol in symbols}
    def message(n, symbol):
        return MarketMessage(topic=f'publicTrade.{symbol}', ts=1000000 + n,
            receipt_mono_ns=10_000_000_000,
            data=[{'T': 1000000 + n, 'p': '100', 'v': '1', 'S': 'Buy'}])
    for symbol, handler in handlers.items():
        await handler(message(1, symbol))
    tasks = list(live._event_tasks)
    assert len(tasks) == len(symbols)
    if cancel != 'before_start':
        await asyncio.sleep(0)  # Start the real task, which sleeps for .02 seconds.
        for symbol, handler in handlers.items():
            await handler(message(2, symbol))  # Coalesce while tasks are sleeping.
    if cancel:
        for task in tasks:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    expected = [r for r in rows if r['event'] != 'replay_input']
    return live, replay, inputs, expected


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [None, 'before_start', 'sleeping'])
async def test_scheduler_replays_coalescing_interleaving_and_cancellation(tmp_path, monkeypatch, cancel):
    live, replay, inputs, expected = await fixture(tmp_path, cancel)
    async def forbidden(*args, **kwargs):
        raise AssertionError('replay must not wait or launch a live task')
    replay._event_evaluation_sleep = forbidden
    replay._launch_event_evaluation = forbidden
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, 'sleep', forbidden)
            patch.setattr(asyncio, 'create_task', forbidden)
            report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=expected)
        assert report['outputsMatch'] and not report['parityReady']
        assert replay.sessions['AAA'].market_snapshot() == live.sessions['AAA'].market_snapshot()
        a, b = replay.sessions['AAA'], live.sessions['AAA']
        assert (a.fast_event_requests, a.fast_event_coalesced, a.fast_event_evaluations,
                a.event_eval_pending) == (b.fast_event_requests, b.fast_event_coalesced,
                                          b.fast_event_evaluations, b.event_eval_pending)
        assert replay._event_evaluation_sleep is forbidden
        assert replay._launch_event_evaluation is forbidden
        assert not replay._event_tasks
        assert not a.event_eval_pending and a.event_eval_owner is None
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
async def test_two_symbol_tasks_keep_independent_contexts(tmp_path):
    live, replay, inputs, expected = await fixture(tmp_path, None, ('AAA', 'BBB'))
    try:
        report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=expected)
        assert report['outputsMatch']
        for symbol in ('AAA', 'BBB'):
            assert replay.sessions[symbol].market_snapshot() == live.sessions[symbol].market_snapshot()
            assert replay.sessions[symbol].fast_event_evaluations == 1
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['resume_id', 'parent', 'outputs'])
async def test_scheduler_detects_validly_hashed_semantic_mismatches(tmp_path, fault):
    from test_offline_segment import rehash
    live, replay, inputs, expected = await fixture(tmp_path, None)
    if fault == 'resume_id':
        next(r for r in inputs if r['kind'] == 'scheduler' and r['body']['phase'] == 'resumed')['body']['taskId'] += 100
    elif fault == 'parent':
        next(r for r in inputs if r['kind'] == 'scope' and r['body'].get('name') == 'event_evaluation')['body']['parentId'] = None
    else:
        expected.append(dict(event='invented', symbol=None, payload={}))
    rehash(inputs)
    driver = OfflineScheduledReplay(replay)
    try:
        with pytest.raises(SegmentMismatch):
            await driver.apply(inputs, expected_events=expected)
        assert driver.failed
        assert replay.input_journal is None and not replay._event_tasks
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
async def test_incomplete_window_fails_and_cleans_suspended_coroutine(tmp_path):
    live, replay, inputs, expected = await fixture(tmp_path, None)
    stop = next(i for i, r in enumerate(inputs) if r['kind'] == 'scheduler' and r['body']['phase'] == 'resumed')
    driver = OfflineScheduledReplay(replay)
    try:
        with pytest.raises(SegmentMismatch, match='unfinished tasks'):
            await driver.apply(inputs[:stop])
        assert driver.failed and replay.input_journal is None
        assert '_launch_event_evaluation' not in replay.__dict__
        assert '_event_evaluation_sleep' not in replay.__dict__
        with pytest.raises(SegmentMismatch, match='previous replay failure'):
            await driver.apply(inputs)
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('capture', [False, True])
async def test_cancel_before_start_allows_next_live_evaluation(tmp_path, capture):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        event_driven_evaluation_enabled=True), capture_inputs=capture)
    engine._session_engaged = lambda session: True
    engine._apply_bootstrap_result('AAA', (None, None, [], [], [], []))
    session = engine.sessions['AAA']
    try:
        for reason in ('first', 'after_cancel'):
            engine._schedule_event_evaluation(session, reason)
            tasks = list(engine._event_tasks)
            assert len(tasks) == 1 and session.event_eval_pending
            tasks[0].cancel()  # No event-loop yield before cancellation.
            await asyncio.gather(*tasks, return_exceptions=True)
            assert not session.event_eval_pending
            assert session.event_eval_owner is None and session.pending_latency_message is None
        assert session.fast_event_coalesced == 0
        # An old done callback must not clear a newer owner's state.
        old, new = object(), object()
        session.event_eval_owner = new
        session.event_eval_pending = True
        engine._complete_event_evaluation('AAA', old)
        assert session.event_eval_pending and session.event_eval_owner is new
        engine._complete_event_evaluation('AAA', new)
        assert not session.event_eval_pending
    finally:
        await engine.close()
