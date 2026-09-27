"""Assemble all fixed PR58 series after measurements; never runs a load itself."""
import argparse,csv,hashlib,json
from collections import Counter
from pathlib import Path
from scalp_bot.offline_benchmark import stats

def load(path):return json.loads(Path(path).read_text())
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def build(root,output):
    root=Path(root);output=Path(output);reports=[];stage_rows=[];failures=[];pairs=[]
    for window in (1,2):
        before=load(root/f'before-window{window}/latency_benchmark.json')
        for version in ('before','after'):
            path=root/f'{version}-window{window}/latency_benchmark.json';r=load(path)
            assert r['source_sha256']==before['source_sha256']
            pressure=load(root/f'{version}-window{window}.pressure.json')
            item=dict(window=window,version=version,report_path=str(path),report_sha256=digest(path),report=r,
                pressure_path=str(root/f'{version}-window{window}.pressure.json'),pressure_sha256=digest(root/f'{version}-window{window}.pressure.json'),
                timer_profile=pressure.get('timer_profile',{'requested_ms':None,'changed':False}),
                idle_before=pressure.get('idle_before',{}).get('stats'),idle_requested=pressure.get('idle_requested',{}).get('stats'),
                pressure_scope='whole series including warmup; per-measured-window samples below when stage timestamps exist')
            item['pressure']={key:stats([x[key] for x in pressure['samples'] if x.get(key) is not None]) for key in ('cpu_percent','available_ram_mb','pages_per_sec','disk_queue','disk_bytes_sec')}
            for run in r['runs']:
                label=dict(window=window,version=version,mode=run['mode'],speed=run['speed'],repeat=run['repeat'])
                guard=dict(absolute_event_loop_pass=run['metrics']['event_loop_lateness_ms']['p99']<=20,adapter_budget_pass=run['metrics'].get('data_to_terminal_ms',run['metrics'].get('data_to_adapter_ms',{})).get('p99'),added_budget_pass=run.get('added_budget_pass'))
                guard['adapter_budget_pass']=guard['adapter_budget_pass']<=250 if guard['adapter_budget_pass'] is not None else None
                run['derived_budget_results']=guard
                for key,value in guard.items():
                    if value is False:failures.append(label|dict(budget=key))
                assert run['recorder_health']['droppedRows']==0 and run['recorder_health']['writerError'] is None
                if run['mode']=='shadow':assert run['minimum_forecast_count_passed']
                for key,value in run['metrics'].items():stage_rows.append(label|dict(metric=key,**value))
                if version=='after':
                    stages=load(run['stages_path']);inputs={x['input_id']:x for x in stages['inputs']};p=stages['predictions']
                    actual=[x for x in p if 'predict_end_ns' in x and 'terminal_ns' in x]
                    ages=[(x['terminal_ns']-x['available_ns'])/1e6 for x in actual]
                    run['execution_delay_diagnostic']=dict(delivered=len(ages),greater_than_label_100ms=sum(x>100 for x in ages),fraction_gt_100ms=sum(x>100 for x in ages)/len(ages) if ages else None,scope='scheduled causal input to terminal adapter, all forecasts; not an exchange fill delay or new labels')
                    bridges=[];missing=0
                    for e in stages['evaluations']:
                        state=inputs.get(e['input_id'],{}).get('market_state_ready_ns')
                        if state is None:missing+=1
                        else:bridges.append((e['evaluation_start_ns']-state)/1e6)
                    run['state_to_evaluation_ms']=stats(bridges);run['evaluations_with_warmup_or_nonmarket_source']=missing
                    stage_rows.append(label|dict(metric='state_ready_to_evaluation_including_trigger_wait_ms',**stats(bridges)))
                    assert len(inputs)==run['events'];assert len(p)==run['submitted']
                    run['stages_sha256']=digest(run['stages_path'])
                    run['measurements_sha256']=digest(run['measurements_path'])
                    run['terminal_counts_verified']=dict(Counter(x['terminal'] for x in p))
                    assert run['terminal_counts_verified']==run['prediction_outcomes']
                    measured_start=min(x['scheduled_ns'] for x in inputs.values())
                    measured_end=max([x['processing_end_ns'] for x in inputs.values()]+[x['terminal_ns'] for x in p if 'terminal_ns' in x])
                    timed=[x for x in pressure['samples'] if measured_start<=x['perf_ns']<=measured_end]
                    run['timed_pressure']={key:stats([x[key] for x in timed if x.get(key) is not None]) for key in ('cpu_percent','available_ram_mb','pages_per_sec','disk_queue','disk_bytes_sec')}
                    previous=next(x for x in before['runs'] if all(x[k]==run[k] for k in ('mode','speed','repeat')))
                    run['ordinary_decisions_identical_to_before']=previous['ordinary_decisions_sha256']==run['ordinary_decisions_sha256']
                    run['ordinary_portfolio_identical_to_before']=previous['portfolio_sha256']==run['portfolio_sha256']
                if run['mode']=='shadow':
                    pairs.append(label|dict(derived_budget_results=guard)|{k:run.get(k) for k in ('forecasts','submitted','minimum_forecast_count_passed','absolute_event_loop_pass','adapter_budget_pass','added_budget_pass','ordinary_decisions_identical','ordinary_portfolio_identical','added_event_loop_p99_ms','coalesced','dropped','delivered_fraction','prediction_outcomes','execution_delay_diagnostic')})
            reports.append(item)
    old=[]
    for name in ('latency-window1-v1.json','latency-window2-v2.json'):
        p=Path('docs/pr58-readiness')/name
        if p.exists():old.append(dict(path=str(p),sha256=digest(p),report=load(p)))
    gc_report=load(root/'gc-probe/gc-diagnosis.json')
    result=dict(schema=1,budgets=dict(loop_p99_ms=20,added_shadow_p99_ms=5,data_to_adapter_p99_ms=250),source_build='6cc28d8',
        comparisons=reports,paired_summary=pairs,failed_budgets=failures,previous_unisolated=old,gc_diagnosis=gc_report,
        interpretation=dict(before='metric v3; unchanged harness, default OS timer; all failures retained; adapter guard derived numerically because v3 did not serialize a pass flag',after='metric v4; immutable archive GC isolation, bounded source queue, linked causal timestamps; Windows timer request1ms in benchmark process only',
        comparable='same source/config/windows/repeats/models/recorder, same heartbeat20ms and additional5ms; legacy data age additionally preserved',
        not_identical='new data age uses last actually applied symbol input, not next input; source delivery has explicit bounded queue. Timer profile differs and is declared, not a launcher change.',
        admission='conditional local performance only; default-profile and live networking admission not established; label100ms is stricter than250ms adapter budget'))
    with (output/'performance-isolated.json').open('x') as f:json.dump(result,f,indent=2)
    with (output/'performance-stages.csv').open('x',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(stage_rows[0]));w.writeheader();w.writerows(stage_rows)
    print(json.dumps(dict(failures=failures,pairs=len(pairs))))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('output');a=p.parse_args();build(a.root,a.output)
