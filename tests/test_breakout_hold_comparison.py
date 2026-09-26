import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from test_e01_live import prepared_smoke


comparison = runpy.run_path('scripts/compare-breakout-hold.py')
replay = runpy.run_path('scripts/replay-e01-breakout-window.py')


@pytest.mark.asyncio
async def test_independent_hold_portfolio_preserves_other_config_and_provenance(tmp_path, monkeypatch):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    await runner.run()
    windows = tmp_path/'windows.json'
    windows.write_text('[]')
    await replay['run'](SimpleNamespace(directory=runner.directory,feed_name='feed.jsonl',
        source_root=Path.cwd(),windows=windows,output_directory=tmp_path/'control'))
    args = SimpleNamespace(directory=runner.directory,feed_name='feed.jsonl',source_root=Path.cwd(),
        baseline_proof=tmp_path/'control'/'result.json',output_directory=tmp_path/'candidate')
    result = await comparison['run'](args)
    assert result['status'] == 'completed_development_comparison'
    assert result['configDifferences'] == ['breakout_hold_without_retest_seconds']
    assert result['candidate']['manifest']['config']['breakout_hold_without_retest_seconds'] == 3
    assert result['baseline']['manifest']['config']['breakout_hold_without_retest_seconds'] == 8
    assert result['candidate']['manifest']['config']['e01_breakout_obstacle_veto'] is False
    assert result['subscriptionLifetimesMatch'] and not result['holdout']
    assert len(result['candidate']['trades']) > 0
    assert result['candidate']['net'] == pytest.approx(sum(t['netPnl'] for t in result['candidate']['trades']))
    # A new comparison must refuse an inconsistent cached control proof.
    proof = json.loads(args.baseline_proof.read_text())
    proof['inputHash'] = 'wrong'
    args.baseline_proof.write_text(json.dumps(proof))
    args.output_directory = tmp_path/'bad'
    with pytest.raises(ValueError, match='verified baseline'):
        await comparison['run'](args)
