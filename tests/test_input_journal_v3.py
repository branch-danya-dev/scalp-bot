import asyncio
import json

import pytest

from scalp_bot.config import Settings
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.input_journal import validate_input_journal, V2_MISSING_COVERAGE
from scalp_bot.input_scope import InputScopes
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.runtime_clock import ClockTape, ClockTapeMismatch, RecordingRuntimeClock, ReplayRuntimeClock
from test_input_journal import capture, write_report


def test_clock_tape_is_strict_and_preserves_exact_observations():
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_001)
    calls = []
    recorded = RecordingRuntimeClock(clock, lambda method, value: calls.append(dict(method=method, value=value)))
    assert recorded.time() == 1000
    clock.set_observation(wall_seconds=900, mono_ns=10_000_000_002)
    expected = (recorded.perf_counter_ns(), recorded.monotonic(), recorded.time())
    tape = ClockTape(calls)
    with pytest.raises(ClockTapeMismatch): tape.perf_counter_ns()
    with pytest.raises(ClockTapeMismatch): tape.assert_exhausted()
    assert tape.time() == 1000  # Failed reads did not consume it.
    assert (tape.perf_counter_ns(), tape.monotonic(), tape.time()) == expected
    tape.assert_exhausted()
    with pytest.raises(ClockTapeMismatch): tape.time()
    invalid = ClockTape([None, dict(method='time', value=1)])
    for _ in range(2):
        with pytest.raises(ClockTapeMismatch): invalid.time()


def test_v2_coverage_is_preserved(tmp_path):
    journal, _, rows = capture()
    journal.close()
    previous = None
    for row in rows:
        p = row['payload']
        p['schema'] = 'replay-input-v2'
        if p['kind'] == 'header': p['body']['missingCoverage'] = list(V2_MISSING_COVERAGE)
        p['previousHash'] = previous
        p['hash'] = fingerprint({k: v for k, v in p.items() if k != 'hash'})
        previous = p['hash']
    report = write_report(tmp_path, rows)
    assert report['structuralStatus'] == 'checks_passed'
    assert 'every_runtime_clock_read' in report['missingCoverage']


@pytest.mark.asyncio
async def test_scopes_follow_async_context_not_one_global_stack(tmp_path):
    journal, clock, rows = capture()
    scopes = InputScopes(journal)
    observed = RecordingRuntimeClock(clock, lambda method, value: journal.append('clock_read', None,
        {'scopeId': scopes.current.get(), 'method': method, 'value': value}))
    release = asyncio.Event()
    async def child(symbol):
        with scopes.enter('evaluate', symbol):
            observed.time()
            await release.wait()
            observed.perf_counter_ns()
    with scopes.enter('market_message', 'ROOT'):
        children = [asyncio.create_task(child(s)) for s in ('AAA', 'BBB')]
        await asyncio.sleep(0)
    # The scheduling parent may end before its children; it is a causal link.
    release.set()
    await asyncio.gather(*children)
    with pytest.raises(RuntimeError):
        with scopes.enter('arbiter'):
            raise RuntimeError('not serialized')
    with pytest.raises(asyncio.CancelledError):
        with scopes.enter('evaluate', 'CCC'):
            raise asyncio.CancelledError()
    journal.close()
    assert write_report(tmp_path, rows)['structuralStatus'] == 'checks_passed'
    bodies = [r['payload']['body'] for r in rows if r['payload']['kind'] == 'scope']
    assert {b['outcome'] for b in bodies if b['phase'] == 'end'} == {'returned', 'raised', 'cancelled'}
    assert all(b['parentId'] == 1 for b in bodies if b['phase'] == 'begin' and b['id'] in (2, 3))


@pytest.mark.parametrize('kind,body,issue', [
    ('scope', {'phase': 'end', 'id': 1, 'outcome': 'returned'}, 'unmatched_scope_end'),
    ('scope', {'phase': 'begin', 'id': 1, 'parentId': 2, 'name': 'arbiter'}, 'invalid_scope_parent_or_id'),
    ('scope', {'phase': 'begin', 'id': 1, 'parentId': None, 'name': 'arbiter'}, 'unclosed_scopes'),
    ('clock_read', {'scopeId': 1, 'method': 'time', 'value': 1}, 'clock_read_outside_scope'),
    ('scheduler', {'phase': 'resumed', 'taskId': 1, 'reason': '', 'delay': 0, 'outcome': None}, 'invalid_scheduler_transition'),
    ('scheduler', {'phase': 'scheduled', 'taskId': 1, 'reason': '', 'delay': 0, 'outcome': None}, 'unfinished_scheduled_tasks'),
])
def test_validator_checks_scope_and_task_lifecycle_not_only_hashes(tmp_path, kind, body, issue):
    journal, _, rows = capture()
    journal.append(kind, 'AAA' if kind == 'scheduler' else None, body)
    journal.close()
    report = write_report(tmp_path, rows)
    assert report['issues'][issue] > 0
    assert report['structuralStatus'] == 'rejected'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_engine_fast_scheduler_coalescing_sleep_and_prestart_cancel(tmp_path, monkeypatch, cancel):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        event_driven_evaluation_enabled=True, event_evaluation_min_interval_seconds=.02),
        clock=ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000), capture_inputs=True)
    session = ActiveSymbolSession(symbol='AAAUSDT', clock=engine.clock)
    session.last_event_eval_at = engine.clock.monotonic()
    engine.sessions[session.symbol] = session
    monkeypatch.setattr(engine, '_session_engaged', lambda s: True)
    try:
        engine._schedule_event_evaluation(session, 'first')
        engine._schedule_event_evaluation(session, 'second')
        tasks = list(engine._event_tasks)
        assert len(tasks) == 1
        if cancel:
            for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await engine.close()
    report = validate_input_journal(engine.recorder.path)
    assert report['structuralStatus'] == 'checks_passed', report
    records = [json.loads(x)['payload'] for x in engine.recorder.path.read_text().splitlines()
               if json.loads(x)['event'] == 'replay_input']
    scheduler = [p['body'] for p in records if p['kind'] == 'scheduler']
    phases = [b['phase'] for b in scheduler]
    assert phases[:2] == ['scheduled', 'coalesced']
    assert phases[-1] == 'finished'
    if cancel:
        assert 'started' not in phases and scheduler[-1]['outcome'] == 'cancelled'
    else:
        assert phases[2:-1] == ['started', 'sleep', 'resumed']
        scopes = [p['body'] for p in records if p['kind'] == 'scope' and p['body']['phase'] == 'begin']
        parent = next(s['id'] for s in scopes if s['name'] == 'event_evaluation')
        assert next(s['parentId'] for s in scopes if s['name'] == 'evaluate') == parent


@pytest.mark.asyncio
async def test_engine_evaluation_replays_clock_calls_exactly(tmp_path):
    source = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    config = Settings(_env_file=None, session_dir=str(tmp_path / 'capture'), exchange_clock_enabled=True)
    capture_engine = TradingEngine(config, clock=source, capture_inputs=True)
    s = ActiveSymbolSession(symbol='AAA', clock=capture_engine.clock)
    capture_engine.sessions[s.symbol] = s
    try:
        await capture_engine._evaluate(session=s)  # Also tests named-argument attribution.
    finally:
        await capture_engine.close()
    rows = [json.loads(x)['payload'] for x in capture_engine.recorder.path.read_text().splitlines()
            if json.loads(x)['event'] == 'replay_input']
    scope = next(p['body']['id'] for p in rows if p['kind'] == 'scope' and p['body'].get('name') == 'evaluate')
    tape = ClockTape(p['body'] for p in rows if p['kind'] == 'clock_read' and p['body']['scopeId'] == scope)
    replay = TradingEngine(config.model_copy(update={'session_dir': str(tmp_path / 'replay')}), clock=tape)
    r = ActiveSymbolSession(symbol='AAA', clock=tape, activated_at=1000, last_ranked_at=1000)
    replay.sessions[r.symbol] = r
    # Recorder metadata is deliberately separate from business-clock calls.
    replay.recorder.clock = source
    try:
        await replay._evaluate(session=r)
        tape.assert_exhausted()
        assert {k: d.public() for k, d in r.decisions.items()} == {k: d.public() for k, d in s.decisions.items()}
    finally:
        # Shutdown is a separate scope, not part of the selected evaluation tape.
        replay.clock = source
        replay.broker.clock = source
        await replay.close()
