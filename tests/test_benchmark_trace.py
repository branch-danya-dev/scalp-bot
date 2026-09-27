import json,subprocess,sys
from types import SimpleNamespace
from pathlib import Path
import pytest
from scalp_bot.offline_benchmark_trace import PipelineTrace

def test_causal_available_time_is_not_reset_by_next_input_or_queue():
    t=PipelineTrace(None,100,1);t.wall_start=1000;t.last_source['X']=(1,110)
    row=t.begin_eval(SimpleNamespace(symbol='X'),legacy_scheduled=2000)
    assert row['available_ns']==1010 and row['input_id']==1
    t.end_eval(row)
    t.begin_apply(dict(symbol='X',sequence=2,processingMonoNs=150))
    row=t.begin_eval(SimpleNamespace(symbol='X'),legacy_scheduled=5000)
    assert row['available_ns']==1050 and row['input_id']==2

def test_runtime_cycles_remain_collectible_after_archive_freeze():
    code='''import gc,weakref
from scalp_bot.offline_benchmark_trace import frozen_archive
class Node: pass
archive=Node();archive.self=archive
with frozen_archive() as info:
    assert gc.isenabled() and info['frozen_objects']>0
    runtime=Node();runtime.self=runtime;ref=weakref.ref(runtime)
    del runtime;gc.collect();assert ref() is None
    assert archive.self is archive
assert gc.get_freeze_count()==0 and gc.isenabled()
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr

def test_coalesced_work_is_explicit_and_not_given_success_latency():
    t=PipelineTrace(None,100,1)
    old=SimpleNamespace(ref=SimpleNamespace(source_sequence=1,symbol='X'))
    worker=SimpleNamespace(latest={'X':(old,'long')},capacity=12)
    t.predictions[1]={'terminal':'pending'}
    new=SimpleNamespace(ref=SimpleNamespace(source_sequence=2,symbol='X'))
    t.submitted(worker,new,{'available_ns':10})
    assert t.predictions[1]['terminal']=='coalesced'
    assert 'terminal_ns' not in t.predictions[1]
