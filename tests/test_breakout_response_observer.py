from io import StringIO
import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace as NS

import pytest

from test_e01_live import prepared_smoke

script = runpy.run_path('scripts/replay-breakout-response.py')
replay = runpy.run_path('scripts/replay-e01-breakout-window.py')


def decision(generation=('resistance', 'g1')):
    return NS(action=NS(value='wait'), reasons=[], details={
        'zoneGeneration': generation, 'state': 'break', 'directionalResponseBps': 20})


def test_observer_anchors_each_actual_episode_and_preserves_sign():
    output = StringIO()
    observer = script['ResponseObserver'](output)
    state = NS(zone_key=('resistance', 'g1'), break_started_at=1)
    observer.observe('A', 10, 1000, state, 100, decision())
    observer.observe('A', 20, 2000, state, 100.1, decision())
    state.break_started_at = 0
    observer.observe('A', 30, 3000, state, 100.1, decision())
    state.break_started_at = 4
    observer.observe('A', 40, 4000, state, 102, decision())
    observer.observe('A', 50, 5000, state, 101.9, decision())
    state.zone_key = ('support', 'g2')
    state.break_started_at = 6
    observer.observe('A', 60, 6000, state, 100, decision(state.zone_key))
    observer.observe('A', 70, 7000, state, 99.9, decision(state.zone_key))
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]['postBreakResponseBps'] is None
    assert rows[1]['postBreakResponseBps'] == pytest.approx(10)
    assert rows[2]['anchorPrice'] == 102 and rows[2]['postBreakResponseBps'] is None
    assert rows[3]['postBreakResponseBps'] < 0
    assert rows[-1]['postBreakResponseBps'] == pytest.approx(10)
    assert observer.counts['episodes'] == 3
    assert observer.counts['resets'] == 1


def test_missing_anchor_and_missing_book_are_not_fabricated():
    output = StringIO()
    observer = script['ResponseObserver'](output)
    state = NS(zone_key=('resistance', 'g1'), break_started_at=1)
    observer.observe('A', 20, 2000, state, 100, decision())
    observer.observe('A', 30, 3000, state, 110, decision())
    state.break_started_at = 4
    observer.observe('A', 40, 4000, state, None, decision())
    observer.observe('A', 50, 5000, state, 110, decision())
    assert all(json.loads(line)['postBreakResponseBps'] is None for line in output.getvalue().splitlines())


@pytest.mark.asyncio
async def test_observation_replay_preserves_every_fixture_output(tmp_path, monkeypatch):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    await runner.run()
    windows = tmp_path/'windows.json'
    windows.write_text('[]')
    control = runner.directory/'baseline-replay-05'
    await replay['run'](NS(directory=runner.directory,feed_name='feed.jsonl',source_root=Path.cwd(),
                           windows=windows,output_directory=control))
    with (runner.directory/'baseline-events.jsonl').open('rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    (control/'entry-evidence.json').write_text(json.dumps({'ledgerSha256':digest}))
    result = await script['run'](NS(directory=runner.directory,feed_name='feed.jsonl',source_root=Path.cwd(),
                                   hold=8,output_directory=tmp_path/'observer'))
    assert result['status'] == 'response_observed_portfolio_matched'
    assert result['counts']['observations'] > 0
    assert result['comparedOutputEvents'] > 0
