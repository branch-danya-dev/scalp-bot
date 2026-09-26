"""Ordinary UI capture: real files, exact portfolio replay and loss rejection."""
import asyncio
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scalp_bot.capture import InputWriter, PaperCapture
from scalp_bot.capture_replay import IndexedInputs, verify_capture
from scalp_bot.config import Settings
from scalp_bot.input_journal import validate_input_journal
from scalp_bot.offline_segment import SegmentMismatch
from scalp_bot.run_manifest import runtime_provenance
from test_offline_portfolio import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('event_driven,beta,exit_kind', [
    (False, False, 'shutdown'), (True, True, 'shutdown'), (True, True, 'duration_elapsed')])
async def test_compressed_capture_replays_actual_breakout(tmp_path, monkeypatch, event_driven, beta, exit_kind):
    live, _, _ = await fixture(tmp_path, monkeypatch, production=True, exit_kind=exit_kind,
        file_capture=True, event_driven=event_driven, beta=beta)
    assert live.recorder.inputs.error is None
    assert live.recorder.inputs.closed
    assert live.recorder.inputs.written == live.recorder.inputs.accepted
    assert live.recorder.inputs.pending_bytes == 0
    assert not live.recorder.health()['droppedRows']
    assert live.broker.total_closed_trades == 1
    manifest = live.recorder.inputs.manifest
    metadata = dict(status='sealed', profile='fixture', inputs=live.recorder.inputs.path.name,
        session=live.recorder.path.name, sourceSha256=manifest['code']['sourceSha256'])
    (tmp_path / 'capture.json').write_text(json.dumps(metadata))
    with zipfile.ZipFile(tmp_path / 'source-at-capture.zip', 'w') as archive:
        archive.writestr('fixture.txt', 'Only the archive hash is checked by baseline replay.')
    report = await verify_capture(tmp_path)
    assert report['status'] == 'baseline_replay_matched'
    assert report['balance'] == live.broker.balance
    assert report['closedTrades'] == 1
    assert not report['isolatedStrategySimulationPerformed']
    assert report['integrity']['source']['compression'] == 'gzip'
    assert not (tmp_path / 'replay-index.sqlite').exists()
    # Exercise the real script entry point, whose sys.path differs from pytest
    # and from python -m uvicorn. Dependency provenance must remain identical.
    import subprocess
    import sys
    checked = subprocess.run([sys.executable, 'scripts/replay-paper-capture.py', str(tmp_path)],
        capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    # A financial output change must not be hidden by the telemetry exclusions.
    rows = [json.loads(line) for line in live.recorder.path.read_text().splitlines()]
    next(r['payload'] for r in rows if r['event'] == 'trade_closed')['fees'] += .01
    live.recorder.path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    with pytest.raises(SegmentMismatch, match='output mismatch'):
        await verify_capture(tmp_path)
    assert json.loads((tmp_path / 'capture-check.json').read_text())['status'] == 'rejected'
    assert not (tmp_path / 'replay-index.sqlite').exists()


def test_input_writer_loss_is_visible_and_file_is_exclusive(tmp_path):
    path = tmp_path / 'inputs.gz'
    writer = InputWriter(path, max_queue_bytes=1)
    writer.record('replay_input', None, dict(kind='footer', body={}))
    writer.close()
    assert 'byte limit' in writer.error
    assert writer.accepted == writer.written == 0
    with pytest.raises(FileExistsError):
        InputWriter(path)
    assert gzip.decompress(path.read_bytes()) == b''


def test_index_slices_scope_lookup_and_fresh_rows(tmp_path):
    path = tmp_path / 'inputs.gz'
    payloads = [dict(kind='scope', body=dict(id=1, phase=phase)) for phase in ('begin', 'end')]
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        for payload in payloads:
            stream.write(json.dumps(dict(payload=payload)) + '\n')
    rows = IndexedInputs.build(path, tmp_path / 'index.sqlite')
    try:
        assert rows.scope_end(1, 1) == 1
        assert rows[1:].scope_end(0, 1) == 0
        assert list(rows[:1]) == payloads[:1]
        changed = rows[0]
        changed['body']['id'] = 42
        assert rows[0]['body']['id'] == 1
        assert rows[-1] == payloads[-1]
        with pytest.raises(IndexError):
            rows[2]
        with pytest.raises(ValueError):
            rows[::2]
    finally:
        rows.connection.close()


def profile_settings(tmp_path, monkeypatch, duration):
    for key in list(__import__('os').environ):
        if key.startswith('SCALP_'):
            monkeypatch.delenv(key)
    root = Path(__file__).resolve().parents[1]
    for path in (root / '.env.example', root / f'.env.paper-capture-{duration}'):
        for raw in path.read_text(encoding='utf-8').splitlines():
            if raw.strip() and not raw.startswith('#'):
                key, value = raw.split('=', 1)
                monkeypatch.setenv(key.strip(), value.strip())
    return Settings(_env_file=None, session_dir=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize('duration,profile,beta', [('1h', 'current-1h', False), ('12h', 'all-12h', True)])
async def test_real_profiles_archive_source_and_lock_composition(tmp_path, monkeypatch, duration, profile, beta):
    config = profile_settings(tmp_path, monkeypatch, duration)
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200 * 1024**3))
    capture = PaperCapture(config, profile)
    try:
        assert capture.engine.strategy_enabled['price_action_hypothesis'] is beta
        assert capture.engine.strategy_enabled['trend_structure'] is beta
        assert capture.public()['strategiesLocked']
        capture.before_start()
        capture.started = True
        with pytest.raises(RuntimeError, match='уже запускалась'):
            capture.before_start()
        with zipfile.ZipFile(tmp_path / 'source-at-capture.zip') as archive:
            assert set(archive.namelist()) == set(capture.manifest['code']['fileHashes'])
            assert all(not n.startswith('.env') for n in archive.namelist())
        with pytest.raises(FileExistsError):
            PaperCapture(config, profile)
    finally:
        await capture.engine.close()
        capture.finish()


@pytest.mark.asyncio
async def test_writer_loss_invalidates_capture_and_stops_trading(tmp_path, monkeypatch):
    config = profile_settings(tmp_path, monkeypatch, '1h')
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200 * 1024**3))
    capture = PaperCapture(config, 'current-1h')
    calls = []
    monkeypatch.setattr(capture.engine, 'set_running', lambda enabled: calls.append(enabled))
    capture.recorder.inputs.error = 'simulated disk failure'
    monitor = asyncio.create_task(capture.monitor())
    try:
        await asyncio.sleep(1.05)
        assert calls == [False]
        assert not capture.public()['startAllowed']
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await capture.engine.close()
        capture.finish()
    assert json.loads((tmp_path / 'capture.json').read_text())['status'] == 'invalid'


def test_runtime_fingerprint_ignores_only_identical_distribution_duplicates(monkeypatch):
    one = SimpleNamespace(metadata={'Name': 'package'}, version='1.0')
    two = SimpleNamespace(metadata={'Name': 'package'}, version='2.0')
    monkeypatch.setattr('scalp_bot.run_manifest.distributions', lambda: [one, one])
    assert runtime_provenance()['packages'] == [dict(name='package', version='1.0')]
    monkeypatch.setattr('scalp_bot.run_manifest.distributions', lambda: [one, two, one])
    assert len(runtime_provenance()['packages']) == 2


@pytest.mark.asyncio
async def test_api_capture_controls_keep_single_start_and_reject_strategy_changes(tmp_path, monkeypatch):
    from fastapi import HTTPException
    import scalp_bot.app as api
    config = profile_settings(tmp_path, monkeypatch, '1h')
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200 * 1024**3))
    capture = PaperCapture(config, 'current-1h')
    monkeypatch.setattr(api, 'capture', capture)
    monkeypatch.setattr(api, 'engine', capture.engine)
    calls = []
    monkeypatch.setattr(capture.engine, 'set_running', lambda value: calls.append(value))
    try:
        with pytest.raises(HTTPException) as failure:
            await api.toggle_strategy('trend_structure', api.ToggleBody(enabled=True))
        assert failure.value.status_code == 409
        assert (await api.start_bot())['ok']
        with pytest.raises(HTTPException) as failure:
            await api.start_bot()
        assert failure.value.status_code == 409
        await api.stop_bot()
        assert calls == [True, False]
    finally:
        await capture.engine.close()
        capture.finish()


def test_truncated_gzip_and_invalid_index_are_not_accepted(tmp_path):
    path = tmp_path / 'inputs.gz'
    path.write_bytes(gzip.compress(b'{"event":"unused"}\n')[:-5])
    with pytest.raises((EOFError, OSError)):
        validate_input_journal(path)
    database = tmp_path / 'index.sqlite'
    with pytest.raises((EOFError, OSError, KeyError)):
        IndexedInputs.build(path, database)
    assert not database.exists()
