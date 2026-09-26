import asyncio
from copy import deepcopy
import json
from pathlib import Path
import runpy
import zipfile

import pytest

from test_e01_live import prepared_smoke


script = runpy.run_path('scripts/analyze-e01-decisions.py')


@pytest.mark.parametrize('side', ['long', 'short'])
def test_tape_windows_use_receipt_boundary_and_report_price_not_profit(side):
    block = dict(monoNs=10_000_000_000, entry=100, side=side,
                 stop=95 if side == 'long' else 105,
                 target=110 if side == 'long' else 90,
                 structuralPath={'firstTakePrice': 105 if side == 'long' else 95})
    window = script['TapeWindow'](block, 60)
    window.add(9_000_000_000, 1000)  # Already received before rejection.
    window.add(10_000_000_000, 1000)
    window.add(11_000_000_000, 105)
    window.add(12_000_000_000, 90)
    window.add(70_000_000_000, 102)
    window.add(70_000_000_001, 1000)
    result = window.result(80_000_000_000)
    assert result['printCount'] == 3
    assert result['horizonMoveBps'] == pytest.approx(200 if side == 'long' else -200)
    assert result['observedFavorableBps'] == pytest.approx(500 if side == 'long' else 1000)
    assert result['observedAdverseBps'] == pytest.approx(1000 if side == 'long' else 500)
    assert result['firstTouchSeconds']['stop'] == (2 if side == 'long' else 1)
    assert result['lastPrintAgeToBoundarySeconds'] == 0
    assert not script['TapeWindow'](block, 300).result(80_000_000_000)['windowComplete']


def test_incomplete_window_does_not_claim_horizon_return():
    block = dict(monoNs=0, entry=100, side='long', stop=95, target=110, structuralPath={})
    window = script['TapeWindow'](block, 300)
    window.add(10_000_000_000, 102)
    report = window.result(60_000_000_000)
    assert report['horizonMoveBps'] is None
    assert report['lastPrintAgeToBoundarySeconds'] == 50


def test_candidate_identity_deduplicates_repeated_signals_not_wait_states(tmp_path):
    rows = []
    def emit(event, payload):
        rows.append(dict(event=event, payload=payload, monoNs=len(rows), wallSeconds=len(rows), symbol='AAA'))
    emit('bot_started', {})
    signal = dict(strategy='level_breakout', action='short', entry=100, stop=105, target=90,
                  setup_id='g1', details={'zoneGeneration':['support','g1'], 'state':'impulse'}, reasons=[])
    emit('decision', signal)
    emit('decision', deepcopy(signal))
    wait = dict(signal, action='wait', setup_id=None, details={'zoneGeneration':['support','g2'], 'state':'armed'})
    emit('decision', wait)
    first_break = dict(wait, details={'zoneGeneration':['support','g2'], 'state':'break'},
                       marketContext={'lastPrice':99}, reasons=['needs confirmation'])
    emit('decision', first_break)
    emit('decision', dict(first_break, marketContext={'lastPrice':98}))
    emit('decision', dict(signal, setup_id='g2'))
    emit('run_summary', {})
    emit('bot_stopped', {})
    path = tmp_path/'ledger.jsonl'
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
    result = script['scan_events'](path)
    assert len(result['signals']) == 2
    assert result['summary']['decisionActions'] == {'short':3, 'wait':3}
    assert result['summary']['generationsByState']['armed'] == 1
    assert len(result['firstBreaks']) == 1
    observation = next(iter(result['firstBreaks'].values()))
    assert observation['entry'] == 99 and observation['side'] == 'short'
    assert observation['stop'] is None and observation['target'] is None


@pytest.mark.asyncio
async def test_real_capture_analysis_binds_manifest_feed_and_ledger(tmp_path, monkeypatch):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    result = await asyncio.wait_for(runner.run(), timeout=10)
    source = tmp_path/'source.zip'
    with zipfile.ZipFile(source, 'x') as archive:
        for name in result['portfolios']['baseline']['manifest']['code']['fileHashes']:
            archive.write(name, name)
    report = script['analyze'](runner.directory, 'feed.jsonl', source)
    assert report['validatedInputs'] == result['eventsConsumed']
    assert report['uniqueBreakoutCandidates'] >= 1
    assert not report['fullReplayPerformed'] and not report['profitabilityProven']
    with (runner.directory/'feed.jsonl').open('a') as f: f.write('{}\n')
    with pytest.raises(ValueError, match='trailing data'):
        script['analyze'](runner.directory, 'feed.jsonl', source)


def test_archive_mismatch_is_rejected(tmp_path):
    path = tmp_path/'bad.zip'
    with zipfile.ZipFile(path, 'x') as archive: archive.writestr('wrong', b'bad')
    hashes = {'expected':'hash'}
    header = {'code':{'fileHashes':hashes, 'sourceSha256':script['fingerprint'](hashes)}}
    with pytest.raises(ValueError, match='file set'):
        script['verify_archive'](header, path)
