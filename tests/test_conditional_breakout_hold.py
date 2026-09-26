from io import StringIO
import json
from pathlib import Path
import runpy
from types import SimpleNamespace as NS

import pytest

from test_e01_live import prepared_smoke

script=runpy.run_path('scripts/compare-conditional-breakout-hold.py')
replay=runpy.run_path('scripts/replay-e01-breakout-window.py')


class Strategy:
    hold_without_retest_seconds=8
    def __init__(self):
        self.pressure={'nearCloses':2,'compressedPullbacks':2}
        self.break_started_at=123
        self.calls=0
        self.fail=False
    def _pressure_score(self,*args,**kwargs): return 4,self.pressure
    def evaluate(self,*args,**kwargs):
        self.calls+=1
        score,pressure=self._pressure_score()
        if self.fail: raise RuntimeError('test error')
        return NS(action=NS(value='wait'),details={'hold':self.hold_without_retest_seconds,'score':score,'pressure':pressure})


def test_threshold_loss_and_symbol_isolation_preserve_timer():
    strategy=Strategy()
    trace=StringIO()
    script['install_overlay'](strategy,NS(perf_counter_ns=lambda:1),trace,True)
    assert strategy.evaluate([],None,None,symbol='A').details['hold']==3
    strategy.pressure={'nearCloses':2,'compressedPullbacks':1}
    assert strategy.evaluate([],None,None,symbol='B').details['hold']==8
    strategy.pressure={'nearCloses':1,'compressedPullbacks':3}
    assert strategy.evaluate([],None,None,symbol='A').details['hold']==8
    strategy.pressure={}
    assert strategy.evaluate([],None,None,symbol='A').details['hold']==8
    assert strategy.break_started_at==123 and strategy.calls==4
    assert strategy.hold_without_retest_seconds==8
    assert [json.loads(l)['holdSeconds'] for l in trace.getvalue().splitlines()]==[3,8,8,8]


def test_control_and_exception_restore_base_hold():
    strategy=Strategy()
    script['install_overlay'](strategy,NS(perf_counter_ns=lambda:1),StringIO(),False)
    assert strategy.evaluate([],None,None).details['hold']==8
    candidate=Strategy()
    script['install_overlay'](candidate,NS(perf_counter_ns=lambda:1),StringIO(),True)
    candidate.fail=True
    with pytest.raises(RuntimeError): candidate.evaluate([],None,None)
    assert candidate.hold_without_retest_seconds==8 and candidate.break_started_at==123


def test_native_retest_contract_with_conditional_overlay(monkeypatch):
    import test_strategy_level_semantics as semantics
    native = semantics.LevelBreakoutStrategy
    def factory():
        strategy = native()
        script['install_overlay'](strategy,NS(perf_counter_ns=lambda:1),StringIO(),True)
        return strategy
    monkeypatch.setattr(semantics,'LevelBreakoutStrategy',factory)
    # Reuse the real price/flow sequence: waiting alone cannot confirm a retest;
    # fresh movement does, with the original retest confirmation mode.
    semantics.test_breakout_retest_requires_fresh_post_retest_response()


@pytest.mark.asyncio
async def test_full_control_parity_candidate_and_finalize(tmp_path,monkeypatch):
    runner,_=await prepared_smoke(tmp_path,monkeypatch)
    await runner.run()
    windows=tmp_path/'windows.json'; windows.write_text('[]')
    await replay['run'](NS(directory=runner.directory,feed_name='feed.jsonl',source_root=Path.cwd(),windows=windows,
                           output_directory=runner.directory/'baseline-replay-05'))
    for mode in ('control','candidate'):
        await script['run'](NS(directory=runner.directory,feed_name='feed.jsonl',source_root=Path.cwd(),mode=mode,
                               output_directory=tmp_path/mode))
    result=script['finalize'](tmp_path/'control',tmp_path/'candidate',tmp_path/'comparison.json')
    assert result['status']=='E06_development_comparison_validated'
    assert result['control']['holdEvaluationCounts'].get('3',0)==0
    assert result['candidate']['holdEvaluationCounts']['3']>0
    assert result['candidate']['baseManifest']['config']['breakout_hold_without_retest_seconds']==8
    assert result['candidate']['scenario']['overlayEnabled']
    assert result['candidate']['baseManifest']['config']['e01_breakout_obstacle_veto'] is False
    path=tmp_path/'candidate'/'scenario.json'
    value=json.loads(path.read_text()); value['rule']='changed'; path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='scenario mismatch'):
        script['finalize'](tmp_path/'control',tmp_path/'candidate',tmp_path/'bad.json')
    path.write_text(json.dumps(result['candidate']['scenario']))
    ledger=tmp_path/'candidate'/'candidate-events.jsonl'
    rows=[json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
    trade=next(row for row in rows if row['event']=='trade_closed')
    trade['payload']['netPnl']+=1
    ledger.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    with pytest.raises(ValueError,match='persisted candidate ledger differs'):
        script['finalize'](tmp_path/'control',tmp_path/'candidate',tmp_path/'bad-ledger.json')
