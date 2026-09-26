import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Candidate, Candle
from scalp_bot.engine import TradingEngine
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_segment import OfflineSegmentReplay, SegmentMismatch
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_offline_segment import segments, rehash


async def fixture(tmp_path, periodic=False):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10*10**9)
    config = Settings(_env_file=None, session_dir=str(tmp_path), event_driven_evaluation_enabled=False,
        working_symbols=1, max_active_symbols=1, active_keep_rank=1,
        active_symbol_min_seconds=1, active_symbol_idle_timeout_seconds=1)
    live = TradingEngine(config, clock=clock, capture_inputs=True)
    prefix = [json.loads(x)['payload'] for x in live.recorder.path.read_text().splitlines()
              if json.loads(x)['event'] == 'replay_input']
    rows, launched = [], []
    updated = asyncio.Event()
    def record(event, symbol, payload):
        rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
        if event == 'scanner_update':
            updated.set()
    live.recorder.record = live.input_journal.record = record
    live._launch_symbol_worker = launched.append  # Isolate transport from scanner state transitions.
    async def instrument(symbol): return None
    async def fees(symbol): return None
    async def candles(symbol, interval, limit):
        return [Candle(900000, 100, 101, 99, 100, 10, 1000)]
    live.rest.instrument_info = instrument
    live.rest.fee_schedule = fees
    live.rest.klines = candles
    candidates = []
    async def ranking(): return deepcopy(candidates)
    live.rest.active_candidates = ranking
    queue = asyncio.Queue()
    async def wait(delay): await queue.get()
    live._periodic_sleep = wait
    task = asyncio.create_task(live._scanner_loop()) if periodic else None
    if task:
        await asyncio.sleep(0)
    for i, symbol in enumerate(('AAA', 'BBB', 'AAA')):
        clock.set_observation(wall_seconds=1000+i*5, mono_ns=(10+i*5)*10**9)
        other = 'BBB' if symbol == 'AAA' else 'AAA'
        candidates = [Candidate(symbol, 2e8, .02, 100, activity_rank=1, mark_price=100+i),
                      Candidate(other, 1e8, .01, 99, activity_rank=2)]
        if task:
            updated.clear()
            queue.put_nowait(None)
            await asyncio.wait_for(updated.wait(), timeout=3)
        else:
            await live._scan_once()
        assert list(live.sessions) == [symbol]
        if i == 1:
            live._apply_context_result(live.sessions[symbol],
                ([Candle(900000, 100, 102, 99, 101, 20, 2000, False)], [], [], []))
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert launched == ['AAA', 'BBB', 'AAA']
    return live, prefix, rows


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter,periodic', [(OfflineSegmentReplay, False),
                                            (OfflineScheduledReplay, False), (OfflineScheduledReplay, True)])
async def test_scanner_promotion_rotation_and_context_use_shared_engine(tmp_path, monkeypatch, adapter, periodic):
    live, prefix, rows = await fixture(tmp_path, periodic)
    replay = restore_cold_engine(prefix)
    driver = adapter(replay)
    async def forbidden(*args, **kwargs): raise AssertionError('unexpected live task or wait')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, 'create_task', forbidden)
            patch.setattr(asyncio, 'sleep', forbidden)
            if periodic:
                inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
                outputs = [r for r in rows if r['event'] != 'replay_input']
                assert (await driver.apply(inputs, expected_events=outputs))['outputsMatch']
            else:
                for inputs, outputs in segments(rows):
                    assert (await driver.apply(inputs, expected_events=outputs))['outputsMatch']
        assert replay.candidates == live.candidates
        assert list(replay.sessions) == ['AAA']
        assert replay.sessions['AAA'].candles == live.sessions['AAA'].candles
        assert replay.sessions['AAA'].mark_price == live.sessions['AAA'].mark_price
        assert list(replay.events) == list(live.events)
        assert not replay._worker_tasks
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['ranking', 'missing_bootstrap', 'bootstrap_symbol'])
async def test_scanner_refuses_changed_or_missing_recorded_inputs(tmp_path, fault):
    live, prefix, rows = await fixture(tmp_path)
    replay = restore_cold_engine(prefix)
    inputs, outputs = segments(rows)[0]
    inputs = deepcopy(inputs)
    if fault == 'ranking':
        next(r for r in inputs if r['kind'] == 'scanner_result')['body']['candidates'].reverse()
    elif fault == 'missing_bootstrap':
        inputs = [r for r in inputs if r['kind'] != 'bootstrap']
    else:
        next(r for r in inputs if r['kind'] == 'bootstrap')['symbol'] = 'WRONG'
    rehash(inputs)
    try:
        with pytest.raises(RuntimeError):
            await OfflineSegmentReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.input_journal is None
    finally:
        await live.close()
        await replay.close()
