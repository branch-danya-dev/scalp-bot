import asyncio
import pytest

from scalp_bot.domain import Candidate
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from test_offline_source_await import fixture
from test_offline_service import fixture as service_fixture
from test_offline_segment import rehash
from test_input_journal import write_report


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['scanner', 'bootstrap'])
async def test_failure_diagnostics_and_recovery_match(tmp_path, monkeypatch, source):
    live, prefix, rows = await fixture(tmp_path, periodic=True, failure=source)
    assert 'BBB' not in live.sessions
    if source == 'scanner':
        assert live._scanner_error == 'ConnectionError: fixture scanner unavailable'
    else:
        assert any(e['event'] == 'symbol_bootstrap_error'
                   and e['payload']['error'] == 'fixture invalid instrument' for e in rows)
    async def ranking():
        return [Candidate('AAA', 2e8, .02, 100), Candidate('BBB', 1e8, .01, 100)]
    async def instrument(symbol): return None
    live.rest.active_candidates = ranking
    live.rest.instrument_info = instrument
    await live._scan_once()
    assert 'BBB' in live.sessions and live._scanner_error is None
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    async def forbidden(*args, **kwargs): raise AssertionError('offline IO')
    try:
        with monkeypatch.context() as patch:
            for name in ('sleep', 'gather', 'create_task'):
                patch.setattr(asyncio, name, forbidden)
            report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
        assert replay._scanner_error == live._scanner_error
        assert replay._last_scan_error_at == live._last_scan_error_at
        assert list(replay.sessions) == list(live.sessions)
        assert list(replay.events) == list(live.events)
    finally:
        await live.close()
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['scanner', 'bootstrap'])
async def test_startup_source_failure_preserves_service_lifecycle(tmp_path, monkeypatch, source):
    live, prefix, inputs, outputs = await service_fixture(tmp_path, monkeypatch, failure=source)
    structural = write_report(tmp_path, [dict(event='replay_input', payload=r) for r in prefix + inputs])
    assert structural['structuralStatus'] == 'checks_passed', structural
    replay = restore_cold_engine(prefix)
    try:
        result = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert result['outputsMatch'] and result['serviceLifecycleMatched']
        assert not replay.sessions
        assert list(replay.events) == list(live.events)
    finally:
        await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['type', 'message', 'source', 'identity'])
async def test_rehashed_failure_diagnostic_changes_are_rejected(tmp_path, fault):
    live, prefix, rows = await fixture(tmp_path, periodic=True, failure='scanner')
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    body = next(r['body'] for r in inputs if r['kind'] == 'source_await' and r['body']['phase'] == 'failed')
    if fault == 'type': body['errorType'] = 'TimeoutError'
    elif fault == 'message': body['errorMessage'] = 'changed diagnostic'
    elif fault == 'source': body['source'] = 'clock'
    else: body['id'] += 1
    rehash(inputs)
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close()
        await replay.close()
