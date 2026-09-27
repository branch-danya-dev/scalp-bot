"""Fixed raw historical loads, warmed worker, repeated off/shadow wall-time benchmark."""
import asyncio
from collections import Counter,defaultdict
from dataclasses import asdict
import hashlib
import json
import os,platform
from pathlib import Path
import time

from .bybit import MarketMessage,OrderBookSequenceError
from .config import Settings
from .domain import Candle,Candidate
from .execution import FeeSchedule
from .instrument import InstrumentSpec
from .offline_segment import _DeniedRest
from .offline_study import StudyEngine,StudyRecorder,advance,source_events
from .runtime_clock import ReplayRuntimeClock
from .recorder import SessionRecorder
from .ml.contracts import SnapshotRef
from .ml.features import FEATURE_SCHEMA,ContextCoverage,extract_context_features
from .ml.worker import InferenceWorker
from .ml.shadow import ShadowAdapter
from .ml.process_stats import process_stats


class BenchmarkRecorder(SessionRecorder):
    def __init__(self,path,clock):
        super().__init__(str(Path(path).parent/Path(path).stem),clock=clock)
        self.sequence=0;self.semantic_hash=hashlib.sha256()
        self.start_background_writer()

    def record(self,event,symbol,payload):
        super().record(event,symbol,payload)
        if event in {'decision','risk_reject','trade_opened','trade_closed','partial_take','entry_cancelled'}:
            self.semantic_hash.update(json.dumps([event,symbol,payload],sort_keys=True,default=str).encode())


async def ingest(engine,handlers,event,arbiter):
    kind,symbol,b=event['kind'],event['symbol'],event['body']
    arbiter=await advance(engine,event['processingMonoNs'],event['processingWallSeconds'],arbiter)
    engine.recorder.sequence=event['sequence']
    if kind=='bootstrap':
        engine._apply_bootstrap_result(symbol,(InstrumentSpec(**b['instrument']),FeeSchedule(**b['fees']),
            *[[Candle(**c) for c in b[k]] for k in ('candles','context5m','context15m','context1h')]))
        handlers[symbol]=engine._market_handler(symbol)
    elif kind=='clock_sample':engine._apply_clock_sample(b)
    elif kind=='clock_error':engine._apply_clock_error(b['errorType'])
    elif kind=='scanner_result':
        engine.candidates=[Candidate(**c) for c in b['candidates']]
        for c in engine.candidates:
            if c.symbol in engine.sessions:
                s=engine.sessions[c.symbol];s.last_ranked_at=engine.clock.time()
                s.mark_price=c.mark_price;s.funding_rate=c.funding_rate;s.next_funding_time_ms=c.next_funding_time_ms
    elif kind=='rest_context' and symbol in engine.sessions:
        engine._apply_context_result(engine.sessions[symbol],tuple(None if b[k] is None else [Candle(**c) for c in b[k]] for k in ('candles','context5m','context15m','context1h')))
    elif kind=='control':
        if b['name']=='set_running':engine.set_running(b['value'])
        elif b['name']=='stop':engine._stop_trading(b['reason'])
        elif b['name']=='toggle_strategy':engine.toggle_strategy(b['key'],b['enabled'])
        else:raise ValueError(b)
    elif kind=='transport' and symbol in handlers:
        engine._invalidate_transport(symbol,b,*handlers[symbol][1:])
    elif kind=='market_message' and symbol in handlers:
        try:await handlers[symbol][0](MarketMessage(**b))
        except OrderBookSequenceError:pass  # Actual handler invalidates book; next snapshot required.
    elif kind=='symbol_lifecycle' and b['action']!='activate':raise ValueError(b)
    return arbiter


def stats(values):
    values=sorted(values)
    return dict(n=len(values),**{k:values[int((len(values)-1)*q)] if values else None for k,q in
        (('p50',.5),('p95',.95),('p99',.99),('max',1))})


async def measure(rows,manifest,output,model,*,shadow,speed,start_ns):
    clock=ReplayRuntimeClock(mono_ns=rows[0]['processingMonoNs'],wall_seconds=rows[0]['processingWallSeconds'])
    recorder=BenchmarkRecorder(output,clock)
    engine=StudyEngine(Settings(_env_file=None,**manifest['config']),clock=clock,recorder=recorder,
                       rest_client=_DeniedRest(),configure_observability=False)
    worker=InferenceWorker(model) if shadow else None
    adapter=ShadowAdapter(json.loads((Path(model)/'manifest.json').read_text())) if shadow else None
    times=defaultdict(list);reasons=Counter();current={};finish={};observed=0;resources=None
    base_evaluate=engine._evaluate;measuring=False;scheduled_at=0;prediction_sequence=0
    async def evaluated(session):
        nonlocal prediction_sequence
        before=time.perf_counter_ns()
        await base_evaluate(session)
        if not measuring:return
        times['evaluate_runtime_ms'].append((time.perf_counter_ns()-before)/1e6)
        if worker and session.market_context and session.instrument:
            context=session.market_context;prediction_sequence+=1
            ref=SnapshotRef('pr58-fixed-load',session.symbol,1,prediction_sequence,
                context.observed_at_ms,scheduled_at,'benchmark-perf-counter',FEATURE_SCHEMA)
            before=time.perf_counter_ns()
            coverage=ContextCoverage((5,15,60),(5,15,60),context.forming_candle is not None,
                session.deep_book_is_fresh(context.observed_at_ms/1000),context.structure is not None,True)
            snapshot=extract_context_features(context,ref,coverage)
            times['state_to_features_ms'].append((time.perf_counter_ns()-before)/1e6)
            times['source_to_features_ms'].append((time.perf_counter_ns()-scheduled_at)/1e6)
            finish[ref.source_sequence]=time.perf_counter_ns()
            current[session.symbol]=ref
            worker.activate(session.symbol,1);worker.submit(snapshot,'long')
    engine._evaluate=evaluated
    stop=asyncio.Event()
    def consume():
        nonlocal observed,resources
        for item in worker.poll() if worker else ():
            if item[0]!='forecast':reasons[item[1]]+=1;continue
            forecast=item[1];received=time.perf_counter_ns();observed+=1;resources=item[3]
            pred_start=forecast.produced_mono_ns-item[2]
            times['queue_and_ipc_ms'].append((pred_start-finish[forecast.source.source_sequence])/1e6)
            times['predict_ms'].append(item[2]/1e6)
            times['return_delivery_ms'].append((received-forecast.produced_mono_ns)/1e6)
            session=engine.sessions[forecast.source.symbol]
            decision,why=adapter.accept(forecast,current[session.symbol],received,quote=session.orderbook.best_ask,
                                  instrument=session.instrument)
            reasons.update(why or ('proposal',))
            times['adapter_runtime_ms'].append((time.perf_counter_ns()-received)/1e6)
            times['data_to_adapter_ms'].append((time.perf_counter_ns()-forecast.source.available_mono_ns)/1e6)
            if decision is not None:times['data_to_proposal_ms'].append((time.perf_counter_ns()-forecast.source.available_mono_ns)/1e6)
    async def monitor():
        while not stop.is_set():
            before=time.perf_counter_ns();deadline=before+1_000_000
            await asyncio.sleep(.001)
            after=time.perf_counter_ns()
            times['heartbeat_sleep_ms'].append((after-before)/1e6)
            times['event_loop_lateness_ms'].append(max(0,after-deadline)/1e6)
            consume()
    arbiter=clock.perf_counter_ns()+int(engine.config.arbiter_interval_seconds*1e9)
    handlers={};task=None
    try:
        warm=[r for r in rows if r['processingMonoNs']<start_ns]
        load=[r for r in rows if r['processingMonoNs']>=start_ns]
        for event in warm:arbiter=await ingest(engine,handlers,event,arbiter)
        if worker:
            worker.start();deadline=time.perf_counter()+30
            while not worker.ready and not worker.failed and time.perf_counter()<deadline:
                worker.poll();await asyncio.sleep(.005)
            if not worker.ready:raise RuntimeError(worker.failed or 'startup timeout')
        resources_before=process_stats();wall_start=time.perf_counter_ns()
        measuring=True;task=asyncio.create_task(monitor())
        for event in load:
            scheduled_at=wall_start+int((event['processingMonoNs']-start_ns)/speed)
            delay=(scheduled_at-time.perf_counter_ns())/1e9
            await asyncio.sleep(max(0,delay))
            before=time.perf_counter_ns()
            arbiter=await ingest(engine,handlers,event,arbiter)
            after=time.perf_counter_ns()
            times['handler_runtime_ms'].append((after-before)/1e6)
            times['source_to_state_ms'].append((after-scheduled_at)/1e6)
            if worker:
                consume()
                times['worker_mailbox_depth'].append(len(worker.latest))
                times['worker_inflight'].append(int(worker.inflight is not None))
        deadline=time.perf_counter()+2
        while worker and (worker.inflight or worker.latest) and time.perf_counter()<deadline:
            consume();await asyncio.sleep(.001)
        measuring=False
        portfolio=dict(closed=engine.broker.closed_trades,balance=engine.broker.balance,
                       positions={k:asdict(v) for k,v in engine.broker.positions.items()})
        report=dict(mode='shadow' if shadow else 'off',speed=speed,events=len(load),forecasts=observed,
            minimum_forecast_count_passed=observed>=1000 if shadow else None,
            duration_seconds=(time.perf_counter_ns()-wall_start)/1e9,metrics={k:stats(v) for k,v in times.items()},
            reasons=dict(reasons),worker=worker.info if worker else None,worker_resources=resources,
            worker_failure=worker.failed if worker else None,coalesced=worker.coalesced if worker else 0,
            dropped=worker.dropped if worker else 0,parent_resources_before=resources_before,parent_resources_after=process_stats(),
            portfolio_sha256=hashlib.sha256(json.dumps(portfolio,sort_keys=True,default=str).encode()).hexdigest(),
            closed_trades=len(engine.broker.closed_trades),balance=engine.broker.balance)
        report['ordinary_decisions_sha256']=recorder.semantic_hash.hexdigest()
        report['recorder_health']=recorder.health()
        report['recorder_type']='production SessionRecorder with background writer; semantic hash instrumentation included'
        observations=Path(output).with_suffix('.measurements.json')
        observations.write_text(json.dumps(dict(times),separators=(',',':'))+'\n')
        report['measurements_path']=str(observations.resolve())
        report['budgets']=dict(event_loop_p99_ms=20,event_loop_p99_added_ms=5,data_to_proposal_p99_ms=250)
        report['absolute_event_loop_pass']=report['metrics']['event_loop_lateness_ms']['p99']<=20
        return report
    finally:
        stop.set()
        if task:await task
        if worker:worker.close()
        recorder.close()


async def main(source,model,output,seconds=60,repeats=3,offset=120):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    rows=[];first=None;manifest=None
    for event in source_events(source):
        if first is None:
            first=event['processingMonoNs'];manifest=event['body']['manifest']
        if event['processingMonoNs']>first+int((offset+seconds)*1e9):break
        rows.append(event)
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    report=dict(metric_schema_version=3,environment=dict(platform=platform.platform(),python=platform.python_version(),logical_cpus=os.cpu_count(),parent_import=str(Path(__file__).resolve()),worker_threads=1),model=json.loads((Path(model)/'manifest.json').read_text())['model_version'],source_sha256=digest,source_start_ns=first+int(offset*1e9),
        historical_seconds=seconds,warmup_seconds=offset,repeats=repeats,
        metric_definition='event_loop_lateness=max(0,actual_wake-(sleep_start+1ms)); heartbeat_sleep_ms retains old duration metric',
        inference_pacing='submit each real evaluation; bounded latest mailbox; no trading await',
        latency_outcome_scope='data_to_adapter includes abstention/rejection; data_to_proposal only emitted proposals; dropped work has no success latency',runs=[])
    for speed in (1,4):
        for repeat in range(repeats):
            pair={}
            for shadow in ((False,True) if repeat%2==0 else (True,False)):
                name=f'{speed}-{repeat}-{int(shadow)}'
                run=await measure(rows,manifest,output/(name+'.jsonl'),model,shadow=shadow,speed=speed,start_ns=first+int(offset*1e9))
                run['repeat']=repeat;pair[run['mode']]=run;report['runs'].append(run)
                print(json.dumps({k:run[k] for k in ('mode','speed','repeat','events','forecasts','duration_seconds','absolute_event_loop_pass')}),flush=True)
                (output/'latency_benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
            off,shadow=pair['off'],pair['shadow']
            shadow['ordinary_portfolio_identical']=off['portfolio_sha256']==shadow['portfolio_sha256']
            shadow['ordinary_decisions_identical']=off['ordinary_decisions_sha256']==shadow['ordinary_decisions_sha256']
            shadow['added_event_loop_p99_ms']=shadow['metrics']['event_loop_lateness_ms']['p99']-off['metrics']['event_loop_lateness_ms']['p99']
            shadow['added_budget_pass']=shadow['added_event_loop_p99_ms']<=5
            (output/'latency_benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('model');p.add_argument('output')
    p.add_argument('--seconds',type=int,default=60);p.add_argument('--repeats',type=int,default=3);p.add_argument('--offset',type=int,default=120);a=p.parse_args()
    asyncio.run(main(a.source,a.model,a.output,a.seconds,a.repeats,a.offset))
