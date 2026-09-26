"""Ordinary UI capture: real files, exact portfolio replay and loss rejection."""
import asyncio
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scalp_bot.capture import InputWriter, PaperCapture
from scalp_bot.capture_replay import IndexedInputs, assess_exam, verify_capture
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
    assert report['strategyResults']['level_breakout']['tradesClosed'] == 1
    assert report['strategyResults']['level_breakout']['netPnl'] == pytest.approx(live.broker.total_pnl)
    assert report['strategyResults']['orderbook_density']['evidenceOnly']
    assert 'exam' not in report
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
@pytest.mark.parametrize('duration,profile,beta', [
    ('1h', 'current-1h', False), ('12h', 'all-12h', True), ('24h', 'current-24h', False)])
async def test_real_profiles_archive_source_and_lock_composition(tmp_path, monkeypatch, duration, profile, beta):
    config = profile_settings(tmp_path, monkeypatch, duration)
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200 * 1024**3))
    capture = PaperCapture(config, profile)
    try:
        assert capture.engine.strategy_enabled['price_action_hypothesis'] is beta
        assert capture.engine.strategy_enabled['trend_structure'] is beta
        assert capture.public()['strategiesLocked']
        metadata = json.loads((tmp_path / 'capture.json').read_text())
        if duration == '24h':
            assert metadata['exam'] == dict(durationSeconds=86400, targetNetReturnFraction=.10)
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
        await capture.close()


def test_24h_keeps_current_profile_trading_and_risk_settings(tmp_path, monkeypatch):
    hour = profile_settings(tmp_path, monkeypatch, '1h').model_dump()
    day = profile_settings(tmp_path, monkeypatch, '24h').model_dump()
    assert day['paper_run_duration_seconds'] == 86400
    assert day['start_balance'] == 1000
    for key in ('paper_run_duration_seconds', 'run_label'):
        hour.pop(key)
        day.pop(key)
    assert day == hour


@pytest.mark.parametrize('balance,elapsed,reason,expected', [
    (1100.0, 86400.1, 'duration_elapsed', 'passed'),
    (1099.999999, 86400.1, 'duration_elapsed', 'failed'),
    (990.0, 86400.1, 'duration_elapsed', 'failed'),
    (1200.0, 3600.0, 'bot_stop', 'incomplete'),
    (1200.0, 86400.1, 'bot_stop', 'incomplete'),
    (1200.0, 86399.9, 'duration_elapsed', 'incomplete'),
])
def test_exam_requires_full_day_and_unrounded_final_net(tmp_path, monkeypatch, balance, elapsed, reason, expected):
    config = profile_settings(tmp_path, monkeypatch, '24h')
    result = assess_exam(config, 'current-24h', dict(balance=balance, elapsedSeconds=elapsed,
        reason=reason, configuredDurationSeconds=86400))
    assert result['status'] == expected
    assert result['targetNetProfit'] == 100
    assert result['netProfit'] == balance - 1000
    assert result['targetBalance'] == 1100


@pytest.mark.parametrize('balance', [float('nan'), float('inf'), -float('inf')])
def test_exam_rejects_nonfinite_balance(tmp_path, monkeypatch, balance):
    config = profile_settings(tmp_path, monkeypatch, '24h')
    with pytest.raises(ValueError, match='invalid exam'):
        assess_exam(config, 'current-24h', dict(balance=balance, elapsedSeconds=86400,
            reason='duration_elapsed', configuredDurationSeconds=86400))


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
        await capture.close()
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
        await capture.close()


def test_truncated_gzip_and_invalid_index_are_not_accepted(tmp_path):
    path = tmp_path / 'inputs.gz'
    path.write_bytes(gzip.compress(b'{"event":"unused"}\n')[:-5])
    with pytest.raises((EOFError, OSError)):
        validate_input_journal(path)
    database = tmp_path / 'index.sqlite'
    with pytest.raises((EOFError, OSError, KeyError)):
        IndexedInputs.build(path, database)
    assert not database.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_kind', ['duration_elapsed', 'bot_stop'])
async def test_stop_seals_real_capture_and_ui_reads_do_not_append_after_footer(tmp_path, monkeypatch, exit_kind):
    import scalp_bot.app as api
    from scalp_bot.engine import TradingEngine
    holder = []
    # Use the short, deterministic market fixture with the real capture owner,
    # monitor, timer, writers and source archive. Production profiles keep 1h/12h.
    monkeypatch.setattr('scalp_bot.capture.validate_profile', lambda *_: dict(freeGiB=0, beta=False))
    def factory(config, clock):
        capture = PaperCapture(config, 'fixture', engine_factory=lambda cfg, **kwargs:
            TradingEngine(cfg, clock=clock, **kwargs))
        holder.append(capture)
        return capture
    live, _, _ = await fixture(tmp_path, monkeypatch, production=True,
        exit_kind=exit_kind, file_capture=True, event_driven=True, capture_factory=factory)
    capture = holder[0]
    monkeypatch.setattr(api, 'capture', capture)
    monkeypatch.setattr(api, 'engine', live)
    assert capture.public()['status'] == 'sealed'
    assert not capture.public()['startAllowed']
    assert json.loads((tmp_path / 'capture.json').read_text())['status'] == 'sealed'
    size = live.recorder.inputs.path.stat().st_size
    sequence = live.input_journal.sequence
    first = await api.state('AAA')
    assert first['market']['symbol'] == 'AAA'
    assert not first['positions'] and not first['botRunning']
    assert len(first['closedTrades']) == 1
    assert first['run']['lastSummary']['reason'] == exit_kind
    first['market']['symbol'] = 'mutated response'
    assert (await api.state('AAA'))['market']['symbol'] == 'AAA'
    assert (await api.state('missing'))['market']['symbol'] == 'AAA'
    await api.stop_bot()
    await asyncio.gather(capture.close(), capture.close())  # later ASGI shutdown
    assert live.input_journal.sequence == sequence
    assert live.recorder.inputs.path.stat().st_size == size
    assert live.recorder.inputs.error is None
    report = await verify_capture(tmp_path)
    assert report['status'] == 'baseline_replay_matched'
    assert report['closedTrades'] == 1
    assert report['balance'] == live.broker.balance


@pytest.mark.asyncio
async def test_capture_close_survives_cancelled_monitor_waiter(tmp_path, monkeypatch):
    config = profile_settings(tmp_path, monkeypatch, '1h')
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200 * 1024**3))
    capture = PaperCapture(config, 'current-1h')
    entered, release = asyncio.Event(), asyncio.Event()
    real_close = capture.engine.close
    calls = []
    async def delayed_close():
        calls.append('close')
        entered.set()
        await release.wait()
        await real_close()
    monkeypatch.setattr(capture.engine, 'close', delayed_close)
    waiter = asyncio.create_task(capture.close())
    await entered.wait()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert not capture.finished
    assert capture.public()['status'] == 'sealing'
    release.set()
    await capture.close()
    assert calls == ['close']
    assert capture.public()['status'] == 'sealed'
    assert capture.recorder.inputs.closed


@pytest.mark.asyncio
async def test_completion_metadata_failure_never_advertises_sealed(tmp_path, monkeypatch):
    config = profile_settings(tmp_path, monkeypatch, '1h')
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200 * 1024**3))
    capture = PaperCapture(config, 'current-1h')
    def cannot_save(_):
        raise OSError('disk unavailable')
    monkeypatch.setattr(capture, '_write_status', cannot_save)
    with pytest.raises(OSError, match='disk unavailable'):
        await capture.close()
    assert capture.recorder.inputs.closed
    assert capture.public()['status'] == 'invalid'
    assert 'metadata' in capture.public()['error']
