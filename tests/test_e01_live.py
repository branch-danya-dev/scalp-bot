import asyncio
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import runpy

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.domain import Candidate, Candle
from scalp_bot.e01_comparison import E01Comparison
from scalp_bot.e01_feed import observations as read_observations, read_header
from scalp_bot.e01_live import LiveSmoke, smoke_settings
import scalp_bot.e01_live as live_module
from test_e01_comparison import observations
from test_offline_portfolio import fixture


def test_smoke_profile_ignores_environment_and_refuses_secrets(monkeypatch):
    monkeypatch.setenv('SCALP_BYBIT_API_KEY', 'must-not-be-used')
    monkeypatch.setenv('SCALP_START_BALANCE', '98765')
    profile = json.loads(Path('configs/e01-smoke.json').read_text())
    config = smoke_settings(profile)
    assert config.start_balance == 1000
    assert config.bybit_api_key.get_secret_value() == ''
    assert config.paper_run_duration_seconds == 1800
    assert config.market_queue_max_lag_seconds == .5
    assert config.research_frame_seconds == 5
    assert config.research_trade_delta_enabled and config.replay_trade_delta_enabled
    with pytest.raises(ValueError): smoke_settings(dict(profile, bybit_api_key='forbidden'))
    with pytest.raises(ValueError): smoke_settings(dict(profile, paper_run_duration_seconds=72*3600))


class FakeTime:
    def __init__(self, row): self.set(row)
    def set(self, row): self.wall, self.mono = row['wallSeconds'], row['monoNs']
    def time(self): return self.wall
    def perf_counter_ns(self): return self.mono
    def perf_counter(self): return self.mono / 1e9


async def prepared_smoke(tmp_path, monkeypatch, *, fault=False):
    engine, _, rows = await fixture(tmp_path, monkeypatch, production=True)
    feed = list(observations(rows))
    scanner = next(r['body'] for r in feed if r['kind'] == 'scanner')
    clock = next(r['body'] for r in feed if r['kind'] == 'clock_sample')
    boots = scanner['bootstrap']['AAA']
    fake_time = FakeTime(feed[0])
    monkeypatch.setattr(live_module, 'time', fake_time)

    class Rest:
        closed = False
        async def active_candidates(self): return [Candidate(**c) for c in scanner['candidates']]
        async def instrument_info(self, symbol): return None
        async def fee_schedule(self, symbol): return None
        async def clock_sample(self): return clock
        async def klines(self, symbol, interval, limit):
            key = {'1': 'candles', '5': 'context5m', '15': 'context15m', '60': 'context1h'}[interval]
            return [Candle(**c) for c in boots[key]]
        async def close(self): self.closed = True

    async def stream(url, symbol, callback, stop, **kwargs):
        for row in feed:
            if row['kind'] == 'start':
                while not runner.pair.started:
                    await asyncio.sleep(0)
                if fault:
                    kwargs['on_transport'](dict(phase='fault', attempt=1, errorType='TestDisconnect', discarded=0, topics=[]))
                    await asyncio.Event().wait()
            elif row['kind'] == 'market_message':
                fake_time.set(row)
                await callback(MarketMessage(**deepcopy(row['body'])))
            elif row['kind'] in ('evaluate', 'arbiter'):
                fake_time.set(row)
                await runner.emit(row['kind'], row['symbol'])
        # Model the explicit deadline; no wall-clock wait in a unit test.
        started = next(iter(runner.pair.engines.values()))._run_started_mono
        delta = started + engine.config.paper_run_duration_seconds - fake_time.perf_counter()
        fake_time.wall += delta
        fake_time.mono += round(delta*1e9)
        await asyncio.Event().wait()

    runner = LiveSmoke(engine.config, tmp_path / 'capture', rest=Rest(), streamer=stream)
    return runner, feed


@pytest.mark.asyncio
async def test_live_collector_seals_replayable_feed_and_real_portfolio(tmp_path, monkeypatch):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    result = await asyncio.wait_for(runner.run(), timeout=10)
    assert result['status'] == 'comparison_completed'
    assert result['longRunReady'] is False
    assert result['equitySamples'] > 0
    assert runner.rest.closed
    performance = json.loads((runner.directory / 'performance.json').read_text())
    assert performance['processing']['market_message']['count'] > 0
    assert performance['recorders']['baseline']['writtenCharacters']['trade_closed'] > 0
    assert all(t.done() for t in runner.tasks)
    assert len(result['portfolios']['baseline']['trades']) == 1
    assert not (runner.directory / 'failure.json').exists()
    script = runpy.run_path('scripts/compare-e01.py')
    replayed = await script['run'](runner.directory / 'feed.jsonl', 100_000)
    assert replayed['sharedInputHash'] == result['sharedInputHash']
    for name in ('baseline', 'candidate'):
        assert replayed['portfolios'][name]['net'] == result['portfolios'][name]['net']
        assert replayed['portfolios'][name]['trades'] == result['portfolios'][name]['trades']
        events = [json.loads(line) for line in (runner.directory / f'{name}-events.jsonl').read_text(encoding='utf-8').splitlines()]
        assert any(r['event'] == 'bot_started' for r in events)
        assert len(runner.recorders[name].rows) == 1  # Full output does not accumulate in memory.
    with pytest.raises(FileExistsError): await runner.run()


@pytest.mark.asyncio
async def test_shutdown_callbacks_cannot_append_after_footer(tmp_path, monkeypatch):
    runner, feed = await prepared_smoke(tmp_path, monkeypatch)
    stream = runner.streamer
    attempted = []

    async def late_callback():
        before = runner.pair.count, runner.pair.input_hash
        row = next(r for r in feed if r['kind'] == 'market_message')
        assert await runner.emit(row['kind'], row['symbol'], row['body']) is None
        assert (runner.pair.count, runner.pair.input_hash) == before
        attempted.append(True)

    async def wrapped_stream(*args, **kwargs):
        try:
            await stream(*args, **kwargs)
        finally:
            await late_callback()

    async def close():
        await late_callback()

    runner.streamer = wrapped_stream
    runner.rest.close = close
    result = await asyncio.wait_for(runner.run(), timeout=10)
    assert len(attempted) == 2
    with (runner.directory / 'feed.jsonl').open(encoding='utf-8') as source:
        header = read_header(source)
        rows = list(read_observations(source, header))
    assert len(rows) == result['eventsConsumed']
    assert rows[-1]['kind'] == 'stop'
    assert not (runner.directory / 'failure.json').exists()


@pytest.mark.asyncio
async def test_disconnect_invalidates_capture_without_success_report(tmp_path, monkeypatch):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch, fault=True)
    with pytest.raises(RuntimeError, match='transport interrupted'):
        await asyncio.wait_for(runner.run(), timeout=10)
    assert runner.rest.closed
    assert not (runner.directory / 'result.json').exists()
    assert json.loads((runner.directory / 'failure.json').read_text())['status'] == 'invalid'
    assert (runner.directory / 'performance.json').exists()
    with (runner.directory / 'feed.jsonl').open(encoding='utf-8') as stream:
        header = read_header(stream)
        with pytest.raises(ValueError, match='missing footer'):
            list(read_observations(stream, header))


@pytest.mark.asyncio
async def test_context_is_shared_but_not_aliased(tmp_path, monkeypatch):
    engine, _, rows = await fixture(tmp_path, monkeypatch, production=True)
    pair = E01Comparison(engine.config)
    feed = list(observations(rows))
    for row in feed: await pair.apply(row)
    candles = next(r['body']['bootstrap']['AAA']['candles'] for r in feed if r['kind'] == 'scanner')
    body = dict(candles=None, context5m=candles, context15m=candles, context1h=candles)
    await pair.apply(dict(feed[-1], kind='rest_context', symbol='AAA', body=body))
    a, b = pair.engines.values()
    assert a.sessions['AAA'].context_5m == b.sessions['AAA'].context_5m
    a.sessions['AAA'].context_5m[0].close += 1
    assert a.sessions['AAA'].context_5m != b.sessions['AAA'].context_5m


@pytest.mark.parametrize('damage', ['drop', 'edit', 'reorder', 'suffix', 'config'])
@pytest.mark.asyncio
async def test_sealed_feed_rejects_damage(tmp_path, monkeypatch, damage):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    await asyncio.wait_for(runner.run(), timeout=10)
    lines = (runner.directory / 'feed.jsonl').read_text(encoding='utf-8').splitlines()
    if damage == 'drop': del lines[2]
    if damage == 'edit': lines[2] = lines[2].replace('scanner', 'changed')
    if damage == 'reorder': lines[1], lines[2] = lines[2], lines[1]
    if damage == 'suffix': lines.append('{}')
    if damage == 'config':
        header = json.loads(lines[0])
        header['config']['start_balance'] += 1
        lines[0] = json.dumps(header)
    stream = StringIO('\n'.join(lines) + '\n')
    header = read_header(stream)
    with pytest.raises(ValueError): list(read_observations(stream, header))


@pytest.mark.asyncio
async def test_union_subscription_keeps_symbol_needed_only_by_candidate(tmp_path, monkeypatch):
    runner, feed = await prepared_smoke(tmp_path, monkeypatch)
    runner.pair = E01Comparison(runner.config)
    for row in feed:
        await runner.pair.apply(row)
    runner.pair.engines['baseline'].sessions.pop('AAA')
    subscribed = []
    async def stream(url, symbol, callback, stop, **kwargs):
        subscribed.append(symbol)
        await asyncio.Event().wait()
    runner.streamer = stream
    await runner.sync_workers()
    await asyncio.sleep(0)
    assert subscribed == ['AAA']
    # Only after both portfolios release the symbol may the shared source stop.
    runner.pair.engines['candidate'].sessions.pop('AAA')
    task = runner.workers['AAA'][0]
    await runner.sync_workers()
    assert task.cancelled() and runner.workers == {}


@pytest.mark.asyncio
async def test_live_context_refresh_uses_shared_rest_result(tmp_path, monkeypatch):
    runner, feed = await prepared_smoke(tmp_path, monkeypatch)
    runner.pair = E01Comparison(runner.config)
    for row in feed: await runner.pair.apply(row)
    captured = []
    async def capture(kind, symbol=None, body=None):
        captured.append((kind, symbol, body))
        await runner.pair.apply(dict(feed[-1], kind=kind, symbol=symbol, body=body))
    runner.emit = capture
    await runner.context()
    assert len(captured) == 1
    assert captured[0][0:2] == ('rest_context', 'AAA')
    assert captured[0][2]['candles'] is None  # Fresh 1m is not re-fetched.


@pytest.mark.asyncio
async def test_reduced_snapshot_frequency_preserves_trading_and_reduces_output(tmp_path, monkeypatch):
    engine, _, rows = await fixture(tmp_path, monkeypatch, production=True)
    config = engine.config.model_copy(update={k: v for k, v in json.loads(Path('configs/e01-smoke.json').read_text()).items()
        if k in ('research_frame_seconds', 'replay_engaged_frame_seconds', 'replay_idle_frame_seconds',
                 'research_trade_delta_enabled', 'replay_trade_delta_enabled')})
    results, frame_counts = [], []
    for settings in (engine.config, config):
        pair = E01Comparison(settings)
        for row in observations(rows): await pair.apply(row)
        results.append(pair.finish())
        frame_counts.append(sum(r['event'] in ('research_frame', 'market_frame')
                                for e in pair.engines.values() for r in e.recorder.rows))
    assert frame_counts[1] < frame_counts[0]
    for name in ('baseline', 'candidate'):
        old, new = (r['portfolios'][name] for r in results)
        assert old['net'] == new['net'] and old['fees'] == new['fees']
        fields = ('symbol', 'strategy', 'side', 'entry', 'exit', 'netPnl', 'fees', 'reason')
        assert [{k: t[k] for k in fields} for t in old['trades']] == [{k: t[k] for k in fields} for t in new['trades']]


def test_first_transport_fault_is_not_overwritten(tmp_path):
    runner = LiveSmoke(smoke_settings(json.loads(Path('configs/e01-smoke.json').read_text())), tmp_path)
    runner.transport_stream = StringIO()
    runner.workers = {s: (None, asyncio.Event()) for s in ('ENAUSDT', 'XPLUSDT')}
    event = dict(phase='fault', attempt=1, errorType='MarketDataBackpressureError', discarded=0, topics=[])
    runner.backpressure('ENAUSDT', dict(topics=[], errorType='MarketDataBackpressureError', message='lag=0.501s'))
    runner.transport('ENAUSDT', event)
    runner.transport('XPLUSDT', event)
    assert 'ENAUSDT' in runner.failure
    assert runner.performance()['backpressure'][0]['message'] == 'lag=0.501s'
