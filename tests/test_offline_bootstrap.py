from copy import deepcopy
import json
from pathlib import Path

import pytest

from scalp_bot.config import Settings
from scalp_bot.input_journal import InputJournal
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_segment import OfflineSegmentReplay, SegmentMismatch
from scalp_bot.research_policy import ResearchPolicyRuntime
from scalp_bot.run_manifest import build_run_manifest, code_provenance
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_manifest_validation import rehash
from test_offline_segment import captured, rehash as rehash_rows


def prefix(config=None, policy=None):
    config = config or Settings(_env_file=None)
    policy = policy or ResearchPolicyRuntime()
    rows = []
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10**10)
    journal = InputJournal(lambda event, symbol, payload: rows.append(payload), clock)
    manifest = build_run_manifest(config, {'level_breakout': True},
        code=code_provenance(Path(__file__).resolve().parents[1]), policy=policy.public())
    journal.append('manifest', None, {'phase': 'capture', 'manifest': manifest})
    journal.append('policy_snapshot', None, {'mode': policy.mode.value, 'manifest': policy.manifest,
        'contentHash': fingerprint(policy.manifest), 'sourceFileSha256': policy.source_sha256})
    return rows


@pytest.mark.asyncio
async def test_restore_ignores_environment_and_never_opens_runtime_resources(tmp_path, monkeypatch):
    config = Settings(_env_file=None, risk_fraction=.0123, session_dir=str(tmp_path / 'never-created'),
                      otel_enabled=True)
    rows = prefix(config)
    monkeypatch.setenv('SCALP_RISK_FRACTION', '0.99')
    monkeypatch.setenv('SCALP_BYBIT_API_KEY', 'CANARY')
    monkeypatch.setenv('SCALP_RESEARCH_POLICY_FILE', 'missing-policy.json')
    def forbidden(*args, **kwargs):
        raise AssertionError('runtime resource opened')
    monkeypatch.setattr('scalp_bot.engine.BybitRestClient', forbidden)
    monkeypatch.setattr('scalp_bot.engine.SessionRecorder', forbidden)
    monkeypatch.setattr('scalp_bot.engine.configure_telemetry', forbidden)
    monkeypatch.setattr(ResearchPolicyRuntime, 'from_settings', forbidden)
    engine = restore_cold_engine(rows)
    try:
        assert engine.config.risk_fraction == .0123
        assert engine.config.otel_enabled  # Preserve config, suppress infrastructure creation.
        assert engine.config.bybit_api_key.get_secret_value() == ''
        assert not (tmp_path / 'never-created').exists()
        assert engine.broker.balance == config.start_balance
        assert not engine.sessions and not engine.running and not engine._tasks
        assert engine.strategy_enabled['level_breakout']
        assert sum(engine.strategy_enabled.values()) == 1
        assert engine.replay_origin['state'] == 'cold' and not engine.replay_origin['parityReady']
        with pytest.raises(SegmentMismatch):
            await engine.start()
        with pytest.raises(SegmentMismatch):
            engine.set_running(True)
        with pytest.raises(SegmentMismatch):
            await engine.rest.clock_sample()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_policy_restored_after_source_file_is_removed(tmp_path):
    from test_engine_lifecycle import policy_file
    path = policy_file(tmp_path, allow_enforce=False)
    policy = ResearchPolicyRuntime.from_file(path, mode='shadow')
    rows = prefix(Settings(_env_file=None, research_policy_mode='shadow', research_policy_file=str(path)), policy)
    Path(path).unlink()
    engine = restore_cold_engine(rows)
    try:
        assert engine.research_policy.public() == policy.public()
        assert engine.research_policy.manifest == policy.manifest
        rows[2]['body']['manifest'].clear()
        assert engine.research_policy.manifest  # No alias to caller-owned mutable JSON.
    finally:
        await engine.close()


@pytest.mark.parametrize('fault', ['source', 'runtime', 'model', 'policy', 'config', 'prefix'])
def test_restore_rejects_incompatible_but_rehashed_prefix(fault):
    rows = prefix()
    manifest = rows[1]['body']['manifest']
    if fault == 'source':
        key = next(iter(manifest['code']['fileHashes']))
        manifest['code']['fileHashes'][key] = 'a' * 64
        manifest['code']['sourceSha256'] = fingerprint(manifest['code']['fileHashes'])
    elif fault == 'runtime':
        manifest['runtime']['python'] = '0.0.0'
        manifest['runtimeSha256'] = fingerprint(manifest['runtime'])
    elif fault == 'model':
        manifest['executionModelVersion'] = 'other-model'
    elif fault == 'policy':
        rows[2]['body']['sourceFileSha256'] = 'a' * 64
    elif fault == 'config':
        del manifest['config']['risk_fraction']
        manifest['configSha256'] = fingerprint(manifest['config'])
    else:
        rows[0]['sequence'] = 2
    rehash(manifest)
    rehash_rows(rows)
    with pytest.raises(SegmentMismatch):
        restore_cold_engine(rows)


@pytest.mark.asyncio
async def test_cold_engine_replays_bootstrap_and_market_without_manual_config(tmp_path):
    live, config, clock, batches = await captured(tmp_path)
    rows = [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
            if json.loads(line)['event'] == 'replay_input']
    engine = restore_cold_engine(rows[:3])
    try:
        driver = OfflineSegmentReplay(engine)
        with pytest.raises(SegmentMismatch, match='not contiguous'):
            await driver.apply(batches[1][0])
        for inputs, expected in batches:
            report = await driver.apply(inputs, expected_events=expected)
            assert report['outputsMatch']
        assert engine.sessions['AAA'].trades == live.sessions['AAA'].trades
        assert engine.sessions['AAA'].orderbook == live.sessions['AAA'].orderbook
    finally:
        await live.close()
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['config', 'balance', 'output', 'strategies', 'policy'])
async def test_cold_origin_cannot_be_silently_changed_or_reused_after_failure(tmp_path, fault):
    live, config, clock, batches = await captured(tmp_path)
    rows = [json.loads(line)['payload'] for line in live.recorder.path.read_text().splitlines()
            if json.loads(line)['event'] == 'replay_input']
    engine = restore_cold_engine(rows[:3])
    if fault == 'config':
        engine.config.risk_fraction *= 2
    elif fault == 'balance':
        engine.broker.balance += 1
    elif fault == 'strategies':
        engine.strategy_enabled['level_breakout'] = not engine.strategy_enabled['level_breakout']
    elif fault == 'policy':
        engine.research_policy.source_sha256 = 'a' * 64
    try:
        with pytest.raises(SegmentMismatch):
            await OfflineSegmentReplay(engine).apply(batches[0][0], expected_events=[])
        if fault == 'output':
            assert engine.replay_origin['state'] == 'failed'
            with pytest.raises(SegmentMismatch, match='discard cold engine'):
                await OfflineSegmentReplay(engine).apply(batches[0][0])
        else:
            assert not engine.sessions
    finally:
        await live.close()
        await engine.close()
