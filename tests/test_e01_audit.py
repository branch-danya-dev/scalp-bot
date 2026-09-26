import asyncio
import json
import runpy

import pytest

from test_e01_live import prepared_smoke


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['net', 'hash', 'failure'])
async def test_completed_capture_audit_reconciles_and_rejects_corruption(tmp_path, monkeypatch, damage):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    result = await asyncio.wait_for(runner.run(), timeout=10)
    audit = runpy.run_path('scripts/audit-e01-capture.py')['audit']
    checked = audit(runner.directory)
    assert checked['status'] == 'capture_and_ledger_checks_passed'
    assert checked['input']['validatedCount'] == result['eventsConsumed']
    assert checked['fullReplayPerformed'] is False
    if damage == 'net': result['portfolios']['baseline']['net'] += 1
    elif damage == 'hash': result['sharedInputHash'] = '0' * 64
    else: (runner.directory / 'failure.json').write_text('{}')
    (runner.directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(ValueError): audit(runner.directory)
