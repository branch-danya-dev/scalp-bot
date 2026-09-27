"""Localize long pauses in the unchanged benchmark; does not patch the bot."""
import argparse,asyncio,gc,hashlib,importlib.util,json,threading,time
from pathlib import Path
import scalp_bot.offline_benchmark as bench

async def run(source,model,output,offset):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    pauses=[];active={};spans=[];run_id='load'
    def observe(phase,info):
        key=(threading.get_ident(),info['generation'])
        if phase=='start':active[key]=time.perf_counter_ns()
        else:pauses.append(dict(run=run_id,generation=info['generation'],thread=key[0],start_ns=active.pop(key),end_ns=time.perf_counter_ns(),collected=info['collected'],uncollectable=info['uncollectable']))
    def sweep():
        a=time.perf_counter_ns();n=gc.collect(2);return dict(ms=(time.perf_counter_ns()-a)/1e6,collected=n,tracked=len(gc.get_objects()))
    empty=[sweep() for _ in range(3)]
    rows=[];first=None;manifest=None
    for event in bench.source_events(source):
        if first is None:first=event['processingMonoNs'];manifest=event['body']['manifest']
        if event['processingMonoNs']>first+int((offset+60)*1e9):break
        rows.append(event)
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    archive=[sweep() for _ in range(3)];start=first+int(offset*1e9)
    original=bench.ingest
    async def traced(engine,handlers,event,arbiter):
        before=time.perf_counter_ns()
        try:return await original(engine,handlers,event,arbiter)
        finally:
            if event['processingMonoNs']>=start:
                spans.append(dict(run=run_id,sequence=event['sequence'],kind=event['kind'],symbol=event['symbol'],topic=event['body'].get('topic'),start_ns=before,end_ns=time.perf_counter_ns(),thread=threading.get_ident()))
    bench.ingest=traced;gc.callbacks.append(observe);reports=[]
    try:
        for repeat in range(3):
            for shadow in ((False,True) if repeat%2==0 else (True,False)):
                run_id=f'{repeat}-{int(shadow)}'
                result=await bench.measure(rows,manifest,output/(run_id+'.jsonl'),model,shadow=shadow,speed=4,start_ns=start)
                reports.append(result);print(json.dumps(dict(run=run_id,loop99=result['metrics']['event_loop_lateness_ms']['p99'],adapter99=result['metrics'].get('data_to_adapter_ms',{}).get('p99'),handler_max=result['metrics']['handler_runtime_ms']['max'])),flush=True)
    finally:
        bench.ingest=original;gc.callbacks.remove(observe)
    slow=[]
    for span in spans:
        duration=(span['end_ns']-span['start_ns'])/1e6
        if duration<50:continue
        overlapping=[p for p in pauses if p['start_ns']<span['end_ns'] and p['end_ns']>span['start_ns']]
        overlap=sum(max(0,min(p['end_ns'],span['end_ns'])-max(p['start_ns'],span['start_ns']))/1e6 for p in overlapping)
        slow.append(dict(**span,duration_ms=duration,gc_overlap_ms=overlap,gc_generations=[p['generation'] for p in overlapping]))
    # No engine is active here. Demonstrate cost of scanning the retained archive.
    before_freeze=[sweep() for _ in range(3)]
    assert gc.get_freeze_count()==0
    gc.freeze()
    try:after_freeze=[sweep() for _ in range(3)];frozen=gc.get_freeze_count()
    finally:gc.unfreeze()
    report=dict(harness_path=bench.__file__,harness_sha256=hashlib.sha256(Path(bench.__file__).read_bytes()).hexdigest(),source_sha256=digest,offset=offset,rows=len(rows),empty_sweeps=empty,loaded_archive_sweeps=archive,before_freeze=before_freeze,after_freeze=after_freeze,frozen_objects=frozen,slow_handlers=slow,runs=reports,scope='extra diagnostic burst repetitions; no production change or GC suppression in timed runs')
    (output/'gc-diagnosis.json').write_text(json.dumps(report,indent=2)+'\n')
    (output/'gc-raw.json').write_text(json.dumps(dict(pauses=pauses,handlers=spans),separators=(',',':'))+'\n')
    print(json.dumps(dict(slow_handlers=len(slow),max_overlap_ms=max((r['gc_overlap_ms'] for r in slow),default=0),loaded_sweep_ms=[r['ms'] for r in archive],frozen_sweep_ms=[r['ms'] for r in after_freeze])),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('model');p.add_argument('output');p.add_argument('--offset',type=int,default=180);p.add_argument('--harness-path');a=p.parse_args()
    if a.harness_path:
        spec=importlib.util.spec_from_file_location('scalp_bot._historical_benchmark',a.harness_path)
        bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)
    asyncio.run(run(a.source,a.model,a.output,a.offset))
