from copy import deepcopy
from io import StringIO
import json
import runpy
from types import SimpleNamespace

import pytest
from pathlib import Path

from test_e01_live import prepared_smoke

script = runpy.run_path('scripts/replay-e01-breakout-window.py')


@pytest.mark.asyncio
async def test_full_baseline_fixture_replay_matches_ledger(tmp_path, monkeypatch):
    runner, _ = await prepared_smoke(tmp_path, monkeypatch)
    expected = await runner.run()
    windows = tmp_path/'windows.json'
    windows.write_text(json.dumps([dict(symbol='AAA', startMonoNs=0, endMonoNs=10**18)]))
    result = await script['run'](SimpleNamespace(directory=runner.directory, feed_name='feed.jsonl',
        source_root=Path.cwd(), windows=windows, output_directory=tmp_path/'replay'))
    assert result['status'] == 'baseline_replay_matched'
    assert result['baselineNet'] == expected['portfolios']['baseline']['net']
    assert result['inputs'] == expected['eventsConsumed']
    assert (tmp_path/'replay'/'probes.jsonl').stat().st_size > 0


def test_comparison_normalizes_json_sequences_and_only_observability_fields():
    left = dict(event='run_summary', payload=dict(manifestId='a', latencyMetrics={'count':1}, balance=1000, values=(1,2)))
    right = dict(event='run_summary', payload=dict(manifestId='b', latencyMetrics={'count':2}, balance=1000, values=[1,2]))
    assert script['event_value'](left) == script['event_value'](right)
    right['payload']['balance'] = 999
    assert script['event_value'](left) != script['event_value'](right)
    left['event'] = right['event'] = 'decision'
    right['payload']['balance'] = 1000
    assert script['event_value'](left) != script['event_value'](right)


def test_checked_recorder_rejects_changed_trading_event(tmp_path):
    clock = SimpleNamespace(time=lambda:1, perf_counter_ns=lambda:2)
    path = tmp_path/'ledger.jsonl'
    row = dict(event='trade_closed',symbol='AAA',payload={'netPnl':3},wallSeconds=1,monoNs=2)
    path.write_text(json.dumps(row)+'\n')
    recorder = script['CheckedRecorder'](path,clock)
    try:
        with pytest.raises(ValueError,match='ledger mismatch'):
            recorder.record('trade_closed','AAA',{'netPnl':4})
    finally: recorder.close()


def test_probes_clone_pre_evaluation_state_without_changing_baseline():
    class Strategy:
        hold_without_retest_seconds = 8
        max_stop_pct = .006
        def __init__(self): self.calls = 0
        def evaluate(self, candles, book, trend, **kwargs):
            self.calls += 1
            fire = self.hold_without_retest_seconds <= 3
            return SimpleNamespace(action=SimpleNamespace(value='short' if fire else 'wait'),
                reasons=[str(self.calls)], entry=100 if fire else None, stop=100.5 if fire else None,
                target=99 if fire else None,
                details={'zone':{'low':99.8,'high':100.2},'zoneGeneration':['support','g1'],'state':'break'})
    strategy = Strategy()
    output = StringIO()
    wrapped = script['probe_wrapper'](strategy,SimpleNamespace(perf_counter_ns=lambda:10),
        [dict(symbol='AAA',startMonoNs=0,endMonoNs=20)],output,lambda candles:1)
    book = SimpleNamespace(best_bid=99.9,best_ask=100.1,spread_pct=.0001)
    for _ in range(2):
        decision = wrapped([],book,None,symbol='AAA',observed_at_ms=123)
        assert decision.action.value == 'wait'
    assert strategy.calls == 2 and strategy.hold_without_retest_seconds == 8
    rows = [json.loads(l) for l in output.getvalue().splitlines()]
    assert rows[0]['probes'][0]['reasons'] == ['1']
    assert rows[1]['probes'][1]['reasons'] == ['2']
    assert rows[0]['structuralStopDistancePct'] == pytest.approx(.004)
