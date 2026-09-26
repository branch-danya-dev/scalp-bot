"""Fixed-composition 12h preparation; no live feed or real-time 12h wait."""
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scalp_bot.capture import PaperCapture, validate_profile, PROFILES
from scalp_bot.capture_replay import verify_capture
from test_paper_capture import profile_settings


def test_current_12h_is_current_hour_except_label_and_duration(tmp_path, monkeypatch):
    hour = profile_settings(tmp_path, monkeypatch, '1h').model_dump()
    twelve = profile_settings(tmp_path, monkeypatch, 'current-12h').model_dump()
    assert twelve['paper_run_duration_seconds'] == 43200
    assert twelve['start_balance'] == 1000
    assert not twelve['trend_structure_enabled']
    assert not twelve['price_action_hypothesis_enabled']
    for key in ['paper_run_duration_seconds', 'run_label']:
        hour.pop(key)
        twelve.pop(key)
    assert hour == twelve
    assert 'targetNetReturnFraction' not in PROFILES['current-12h']
    # Historical full-composition profile remains available, never silently replaced.
    assert PROFILES['all-12h']['trend'] and PROFILES['all-12h']['beta']


@pytest.mark.parametrize('changed', [dict(paper_run_duration_seconds=86400),
    dict(trend_structure_enabled=True), dict(price_action_hypothesis_enabled=True),
    dict(fee_rate_mode='account_if_available'), dict(exchange_clock_enabled=False)])
def test_current_12h_rejects_wrong_composition_or_duration(tmp_path, monkeypatch, changed):
    cfg = profile_settings(tmp_path, monkeypatch, 'current-12h').model_copy(update=changed)
    with pytest.raises(ValueError):
        validate_profile(cfg, 'current-12h')


async def test_current_12h_archives_real_settings_and_locks_single_portfolio(tmp_path, monkeypatch):
    cfg = profile_settings(tmp_path, monkeypatch, 'current-12h')
    monkeypatch.setattr('scalp_bot.capture.shutil.disk_usage', lambda _: SimpleNamespace(free=200*1024**3))
    cap = PaperCapture(cfg, 'current-12h')
    try:
        assert cap.engine.strategy_enabled == {
            'level_breakout': True, 'weak_level_rejection': True,
            'orderbook_density': True, 'trend_structure': False, 'price_action_hypothesis': False}
        assert cap.manifest['config']['paper_run_duration_seconds'] == 43200
        assert cap.manifest['config']['start_balance'] == 1000
        assert cap.public()['strategiesLocked']
        cap.before_start()
        cap.started = True
        with pytest.raises(RuntimeError):
            cap.before_start()
        assert 'exam' not in json.loads((tmp_path/'capture.json').read_text())
    finally:
        await cap.close()
    assert cap.finished and cap.recorder.inputs.closed


def test_current_12h_launcher_selects_the_new_profile_only():
    root = Path(__file__).resolve().parents[1]
    command = (root/'scripts/run-paper-capture-current-12h.ps1').read_text()
    assert '-Profile "current-12h"' in command and '-Check:$Check' in command
    for script in ['run-paper-capture.ps1', 'check-paper-capture.ps1']:
        assert '"current-12h"' in (root/'scripts'/script).read_text()


async def test_virtual_12h_deadline_closes_position_seals_and_replays(tmp_path, monkeypatch):
    from scalp_bot.engine import TradingEngine
    from test_offline_portfolio import fixture
    captures = []
    real_validation = validate_profile
    def timer_fixture_validation(cfg, profile):
        if profile == 'timer-fixture-12h':
            assert cfg.paper_run_duration_seconds == 43200
            return dict(freeGiB=0, beta=False)
        return real_validation(cfg, profile)
    monkeypatch.setattr('scalp_bot.capture.validate_profile', timer_fixture_validation)
    def factory(config, clock):
        cap = PaperCapture(config, 'timer-fixture-12h', engine_factory=lambda cfg, **kwargs:
                           TradingEngine(cfg, clock=clock, **kwargs))
        captures.append(cap)
        return cap
    live, _, _ = await fixture(tmp_path, monkeypatch, production=True,
        file_capture=True, event_driven=True, exit_kind='duration_elapsed',
        capture_factory=factory, duration_seconds=43200)
    cap = captures[0]
    assert cap.public()['status'] == 'sealed'
    assert live._last_run_summary['elapsedSeconds'] == 43200
    assert live._last_run_summary['configuredDurationSeconds'] == 43200
    assert live._last_run_summary['reason'] == 'duration_elapsed'
    assert live.broker.total_closed_trades == 1
    assert not live.broker.positions and not live.broker.pending_entries
    assert live.broker.closed_trades[-1]['reason'] == 'duration_elapsed'
    await cap.close()  # Shutdown after sealing is idempotent.
    report = await verify_capture(tmp_path)
    assert report['status'] == 'baseline_replay_matched'
    assert report['balance'] == live.broker.balance
    assert report['closedTrades'] == 1
    assert 'exam' not in report
