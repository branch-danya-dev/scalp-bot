import asyncio
from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage, _stream_topics
from scalp_bot.config import Settings
from scalp_bot.engine import TradingEngine
from scalp_bot.input_journal import validate_input_journal, V3_MISSING_COVERAGE
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.research_policy import ResearchPolicyRuntime
from test_input_journal import capture, write_report
from test_engine_lifecycle import policy_file
from test_manifest_validation import example


def inputs(path):
    return [r['payload'] for line in path.read_text().splitlines()
            if (r := json.loads(line))['event'] == 'replay_input']


def test_v3_read_compatibility(tmp_path):
    journal, _, rows = capture()
    journal.close()
    previous = None
    for row in rows:
        p = row['payload']
        p['schema'] = 'replay-input-v3'
        if p['kind'] == 'header': p['body']['missingCoverage'] = list(V3_MISSING_COVERAGE)
        p['previousHash'] = previous
        p['hash'] = fingerprint({k: v for k, v in p.items() if k != 'hash'})
        previous = p['hash']
    report = write_report(tmp_path, rows)
    assert report['structuralStatus'] == 'checks_passed'
    assert 'periodic_and_external_dispatch' in report['missingCoverage']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_periodic_wait_and_external_ui_are_captured(tmp_path, monkeypatch, cancel):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path)), capture_inputs=True)
    try:
        if cancel:
            task = asyncio.create_task(engine._input_sleep('context', 100))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            await engine._input_sleep('context', 0)
        engine.public_state(selected_symbol='ABSENTUSDT')
    finally:
        await engine.close()
    report = validate_input_journal(engine.recorder.path)
    assert report['structuralStatus'] == 'checks_passed'
    assert report['policySnapshotChecked']
    rows = inputs(engine.recorder.path)
    phases = [p['body']['phase'] for p in rows if p['kind'] == 'dispatch']
    assert phases == ['wait', 'cancelled' if cancel else 'wake']
    assert next(p['body']['selectedSymbol'] for p in rows if p['kind'] == 'external') == 'ABSENTUSDT'
    scopes = [p['body'] for p in rows if p['kind'] == 'scope' and p['body']['phase'] == 'begin']
    root = next(s['id'] for s in scopes if s['name'] == 'public_state')
    assert any(s['name'] == 'market_health' and s['parentId'] == root for s in scopes)


@pytest.mark.asyncio
async def test_transport_records_fault_drain_and_reconnect_without_error_text(monkeypatch):
    stop = asyncio.Event()
    events = []
    connections = []
    class Socket:
        async def send(self, *args, **kwargs): pass
        async def recv(self, **kwargs):
            if len(connections) == 1:
                raise ConnectionError('SENSITIVE-TRANSPORT-TEXT')
            stop.set()
            return b'{"success":true}'
    class Connect:
        async def __aenter__(self):
            connections.append(1)
            return Socket()
        async def __aexit__(self, *args): return False
    async def no_delay(delay): pass
    monkeypatch.setattr('scalp_bot.bybit.websockets.connect', lambda *a, **k: Connect())
    monkeypatch.setattr('scalp_bot.bybit.asyncio.sleep', no_delay)
    async def callback(message): pass
    await _stream_topics('wss://example.invalid', ['orderbook.50.AAA'], callback, stop,
                         on_transport=events.append)
    assert [(e['attempt'], e['phase']) for e in events] == [
        (1, 'connecting'), (1, 'subscription_sent'), (1, 'fault'), (1, 'drained'),
        (2, 'connecting'), (2, 'subscription_sent'), (2, 'drained')]
    assert events[2]['errorType'] == 'ConnectionError'
    assert 'SENSITIVE-TRANSPORT-TEXT' not in json.dumps(events)


@pytest.mark.asyncio
async def test_worker_transport_snapshots_full_sequencer_state(tmp_path, monkeypatch):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path)), capture_inputs=True)
    from scalp_bot.engine import ActiveSymbolSession
    engine.sessions['AAA'] = ActiveSymbolSession(symbol='AAA', clock=engine.clock)
    async def stream(url, symbol, callback, stop, **kwargs):
        notify = kwargs['on_transport']
        def event(phase):
            notify(dict(phase=phase, attempt=1, topics=['orderbook.50.AAA'], errorType=None, discarded=0))
        event('connecting')
        event('subscription_sent')
        await callback(MarketMessage(topic='orderbook.50.AAA', type='snapshot', ts=1000,
            data={'u': 7, 'seq': 42, 'b': [['100', '2']], 'a': [['101', '3']]}))
        event('drained')
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', stream)
    monkeypatch.setattr(engine, '_schedule_event_evaluation', lambda *a, **k: None)
    try:
        await engine._symbol_worker('AAA', engine._stop)
    finally:
        await engine.close()
    report = validate_input_journal(engine.recorder.path)
    assert report['structuralStatus'] == 'checks_passed', report
    states = [p['body'] for p in inputs(engine.recorder.path) if p['kind'] == 'transport']
    assert states[0]['fastState']['synced'] is False
    assert states[-1]['fastState'] == {'depth': 50, 'synced': True, 'updateId': 7,
        'seq': 42, 'bids': [[100, 2]], 'asks': [[101, 3]]}
    assert states[-1]['deepState']['synced'] is False


@pytest.mark.asyncio
async def test_policy_artifact_can_be_loaded_without_source_file(tmp_path):
    policy_path = policy_file(tmp_path, allow_enforce=False)
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path / 'capture'),
        research_policy_file=str(policy_path), research_policy_mode='shadow'), capture_inputs=True)
    try:
        original = deepcopy(engine.research_policy.manifest)
    finally:
        await engine.close()
    snapshot = next(p['body'] for p in inputs(engine.recorder.path) if p['kind'] == 'policy_snapshot')
    restored = ResearchPolicyRuntime(mode=snapshot['mode'], manifest=snapshot['manifest'],
                                    source_sha256=snapshot['sourceFileSha256'])
    assert restored.manifest == original and restored.active
    assert validate_input_journal(engine.recorder.path)['structuralStatus'] == 'checks_passed'
    broken = deepcopy(snapshot)
    broken['contentHash'] = 'a' * 64
    journal, _, _ = capture()
    with pytest.raises(ValueError): journal.append('policy_snapshot', None, broken)


@pytest.mark.parametrize('phase,expected', [('wake', 'unmatched_dispatch_resume'), ('wait', 'unfinished_dispatch_waits')])
def test_dispatch_pairs_are_checked_independently_of_hash_chain(tmp_path, phase, expected):
    journal, _, rows = capture()
    journal.append('dispatch', None, dict(source='clock', phase=phase, id=1, delay=20))
    journal.close()
    assert write_report(tmp_path, rows)['issues'][expected]


def test_reconnect_requires_previous_attempt_to_be_drained(tmp_path):
    journal, _, rows = capture()
    state = dict(depth=50, synced=False, updateId=None, seq=None, bids=[], asks=[])
    base = dict(workerId=1, topics=['orderbook.50.AAA'], errorType=None, discarded=0,
                fastState=state, deepState=state)
    journal.append('transport', 'AAA', dict(base, phase='connecting', attempt=1))
    journal.append('transport', 'AAA', dict(base, phase='subscription_sent', attempt=1))
    journal.append('transport', 'AAA', dict(base, phase='connecting', attempt=2))
    journal.close()
    assert write_report(tmp_path, rows)['issues']['invalid_transport_transition']


def test_policy_source_binding_is_checked_with_valid_content_hash(tmp_path):
    journal, _, rows = capture()
    journal.append('manifest', None, {'phase': 'capture', 'manifest': example()})
    journal.append('policy_snapshot', None, {'mode': 'off', 'manifest': None,
        'contentHash': fingerprint(None), 'sourceFileSha256': 'a' * 64})
    journal.close()
    assert write_report(tmp_path, rows)['issues']['policy_manifest_binding_mismatch']
