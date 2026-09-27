"""Fixed raw historical loads, warmed worker, repeated off/shadow wall-time benchmark."""
import asyncio
import gc
from .offline_benchmark_trace import PipelineTrace,frozen_archive
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
    trace=getattr(engine,'_benchmark_trace',None)
    if trace:trace.begin_apply(event)
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
    if trace:trace.end_apply(event)
    return arbiter


def stats(values):
    values=sorted(values)
    return dict(n=len(values),**{k:values[int((len(values)-1)*q)] if values else None for k,q in
        (('p50',.5),('p95',.95),('p99',.99),('max',1))})


async def measure(rows,manifest,output,model,*,shadow,speed,start_ns):
    # Remove garbage from previous completed engines outside the measured window.
    gc.collect()
    clock=ReplayRuntimeClock(mono_ns=rows[0]['processingMonoNs'],wall_seconds=rows[0]['processingWallSeconds'])
    recorder=BenchmarkRecorder(output,clock)
    engine=StudyEngine(Settings(_env_file=None,**manifest['config']),clock=clock,recorder=recorder,
                       rest_client=_DeniedRest(),configure_observability=False)
    trace=PipelineTrace(engine,start_ns,speed);trace.install();engine._benchmark_trace=trace
    worker=InferenceWorker(model) if shadow else None
    if worker:trace.bind_worker(worker)
    adapter=ShadowAdapter(json.loads((Path(model)/'manifest.json').read_text())) if shadow else None
    times=defaultdict(list);reasons=Counter();current={};finish={};observed=0;resources=None
    base_evaluate=engine._evaluate;measuring=False;scheduled_at=0;prediction_sequence=0
    async def evaluated(session):
        nonlocal prediction_sequence
        before=time.perf_counter_ns()
        row=trace.begin_eval(session,scheduled_at) if measuring else None
        await base_evaluate(session)
        if not measuring:return
        trace.end_eval(row)
        times['evaluate_runtime_ms'].append((time.perf_counter_ns()-before)/1e6)
        if worker and session.market_context and session.instrument:
            context=session.market_context;prediction_sequence+=1
            ref=SnapshotRef('pr58-fixed-load',session.symbol,1,prediction_sequence,
                context.observed_at_ms,row['available_ns'],'benchmark-perf-counter',FEATURE_SCHEMA)
            row['feature_start_ns']=time.perf_counter_ns()
            coverage=ContextCoverage((5,15,60),(5,15,60),context.forming_candle is not None,
                session.deep_book_is_fresh(context.observed_at_ms/1000),context.structure is not None,True)
            snapshot=extract_context_features(context,ref,coverage)
            row['feature_end_ns']=time.perf_counter_ns()
            times['state_to_features_ms'].append((row['feature_end_ns']-row['feature_start_ns'])/1e6)
            times['source_to_features_ms'].append((row['feature_end_ns']-ref.available_mono_ns)/1e6)
            finish[ref.source_sequence]=row['feature_end_ns'];current[session.symbol]=ref
            worker.activate(session.symbol,1);trace.submitted(worker,snapshot,row)
            if not worker.submit(snapshot,'long'):trace.predictions[prediction_sequence]['terminal']='submit_rejected'
    engine._evaluate=evaluated
    stop=asyncio.Event()
    def consume():
        nonlocal observed,resources
        pending=worker.inflight if worker else None
        for item in worker.poll() if worker else ():
            received=time.perf_counter_ns()
            if item[0]!='forecast':
                reasons[item[1]]+=1
                if pending and pending[0].source_sequence in trace.predictions:
                    trace.predictions[pending[0].source_sequence].update(received_ns=received,terminal_ns=received,terminal='worker_error:'+item[1])
                continue
            forecast=item[1];observed+=1;resources=item[3];ident=forecast.source.source_sequence
            prediction=trace.predictions[ident]
            pred_start=forecast.produced_mono_ns-item[2]
            prediction.update(predict_start_ns=pred_start,predict_end_ns=forecast.produced_mono_ns,received_ns=received)
            times['queue_and_ipc_ms'].append((pred_start-finish[ident])/1e6)
            times['predict_ms'].append(item[2]/1e6)
            times['return_delivery_ms'].append((received-forecast.produced_mono_ns)/1e6)
            session=engine.sessions[forecast.source.symbol]
            decision,why=adapter.accept(forecast,current[session.symbol],received,quote=session.orderbook.best_ask,instrument=session.instrument)
            terminal=time.perf_counter_ns();reasons.update(why or ('proposal',))
            prediction.update(adapter_start_ns=received,adapter_end_ns=terminal,terminal_ns=terminal,terminal='proposal' if decision is not None else '|'.join(why))
            times['adapter_runtime_ms'].append((terminal-received)/1e6)
            times['data_to_adapter_ms'].append((terminal-forecast.source.available_mono_ns)/1e6)
            times['legacy_data_to_adapter_ms'].append((terminal-prediction['legacy_available_ns'])/1e6)
            if decision is not None:times['data_to_proposal_ms'].append((terminal-forecast.source.available_mono_ns)/1e6)
    async def monitor():
        while not stop.is_set():
            before=time.perf_counter_ns();deadline=before+1_000_000
            await asyncio.sleep(.001)
            after=time.perf_counter_ns()
            times['heartbeat_sleep_ms'].append((after-before)/1e6)
            times['event_loop_lateness_ms'].append(max(0,after-deadline)/1e6)
            consume()
    arbiter=clock.perf_counter_ns()+int(engine.config.arbiter_interval_seconds*1e9)
    handlers={};task=None;producer=None
    try:
        warm=[r for r in rows if r['processingMonoNs']<start_ns]
        load=[r for r in rows if r['processingMonoNs']>=start_ns]
        for event in warm:arbiter=await ingest(engine,handlers,event,arbiter)
        if hasattr(engine, "benchmark_warmup_complete"):
            engine.benchmark_warmup_complete()
        if worker:
            worker.start();deadline=time.perf_counter()+30
            while not worker.ready and not worker.failed and time.perf_counter()<deadline:
                worker.poll();await asyncio.sleep(.005)
            if not worker.ready:raise RuntimeError(worker.failed or 'startup timeout')
        resources_before=process_stats();wall_start=time.perf_counter_ns();trace.wall_start=wall_start
        measuring=True;trace.enabled=True;task=asyncio.create_task(monitor())
        queue=asyncio.Queue(maxsize=engine.config.market_queue_size)
        async def release():
            # Separate source delivery from queued handler work. Same fixed deadlines,
            # original order and capacity as configured; full queues backpressure.
            i=0
            while i<len(load):
                await asyncio.sleep(max(0,(trace.scheduled(load[i]['processingMonoNs'])-time.perf_counter_ns())/1e9))
                while i<len(load) and trace.scheduled(load[i]['processingMonoNs'])<=time.perf_counter_ns():
                    event=load[i];r=trace.delivered(event,time.perf_counter_ns())
                    await queue.put(event)
                    r['enqueue_ns']=time.perf_counter_ns();r['queue_depth']=queue.qsize();i+=1
            await queue.put(None)
        producer=asyncio.create_task(release())
        while (event:=await queue.get()) is not None:
            scheduled_at=trace.scheduled(event['processingMonoNs']);trace.processing(event)
            before=time.perf_counter_ns();arbiter=await ingest(engine,handlers,event,arbiter);after=time.perf_counter_ns()
            times['handler_runtime_ms'].append((after-before)/1e6)
            times['source_to_state_ms'].append((after-scheduled_at)/1e6)
            if worker:
                consume();times['worker_mailbox_depth'].append(len(worker.latest));times['worker_inflight'].append(int(worker.inflight is not None))
            await asyncio.sleep(0)
        await producer
        deadline=time.perf_counter()+2
        while worker and (worker.inflight or worker.latest) and time.perf_counter()<deadline:
            consume();await asyncio.sleep(.001)
        measuring=False;trace.enabled=False
        duration=(time.perf_counter_ns()-wall_start)/1e9
        resources_after=process_stats();trace.add_metrics(times)
        assert len(trace.inputs)==len(load), 'raw event omission'
        portfolio=dict(closed=engine.broker.closed_trades,balance=engine.broker.balance,positions={k:asdict(v) for k,v in engine.broker.positions.items()})
        report=dict(mode='shadow' if shadow else 'off',speed=speed,events=len(load),forecasts=observed,submitted=prediction_sequence,
            minimum_forecast_count_passed=observed>=1000 if shadow else None,duration_seconds=duration,
            metrics={k:stats(v) for k,v in times.items()},reasons=dict(reasons),prediction_outcomes=dict(Counter(p['terminal'] for p in trace.predictions.values())),
            delivered_fraction=observed/prediction_sequence if prediction_sequence else None,
            worker=worker.info if worker else None,worker_resources=resources,worker_failure=worker.failed if worker else None,
            coalesced=worker.coalesced if worker else 0,dropped=worker.dropped if worker else 0,
            parent_resources_before=resources_before,parent_resources_after=resources_after,
            portfolio_sha256=hashlib.sha256(json.dumps(portfolio,sort_keys=True,default=str).encode()).hexdigest(),
            closed_trades=len(engine.broker.closed_trades),balance=engine.broker.balance,
            ordinary_decisions_sha256=recorder.semantic_hash.hexdigest(),recorder_health=recorder.health(),source_queue_capacity=queue.maxsize,
            recorder_type='production SessionRecorder/background writer; semantic hashing retained',
            budgets=dict(event_loop_p99_ms=20,event_loop_p99_added_ms=5,data_to_adapter_p99_ms=250),
            all_results_in_latency='all forecasts including stale adapter rejections; worker errors additionally in data_to_terminal, no rejected response discarded',
            source_clock_scope='real scheduled deadline preserved through generator/queue; periodic evaluation uses last applied symbol input, not next event')
        measurements=Path(output).with_suffix('.measurements.json');measurements.write_text(json.dumps(dict(times),separators=(',',':'))+'\n')
        stages=Path(output).with_suffix('.stages.json');stages.write_text(json.dumps(trace.public(),separators=(',',':'))+'\n')
        report.update(measurements_path=str(measurements.resolve()),stages_path=str(stages.resolve()),absolute_event_loop_pass=report['metrics']['event_loop_lateness_ms']['p99']<=20)
        terminals=report['metrics'].get('data_to_terminal_ms',{}).get('p99')
        report['adapter_budget_pass']=terminals<=250 if terminals is not None else None
        return report
    finally:
        stop.set()
        if producer and not producer.done():
            producer.cancel()
            try:await producer
            except asyncio.CancelledError:pass
        if task:await task
        if worker:worker.close()
        trace.close();recorder.close()


async def main(source,model,output,seconds=60,repeats=3,offset=120):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    rows=[];first=None;manifest=None;loading=time.perf_counter()
    for event in source_events(source):
        if first is None:first=event['processingMonoNs'];manifest=event['body']['manifest']
        if event['processingMonoNs']>first+int((offset+seconds)*1e9):break
        rows.append(event)
    preload_seconds=time.perf_counter()-loading
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    report=dict(metric_schema_version=4,environment=dict(platform=platform.platform(),python=platform.python_version(),logical_cpus=os.cpu_count(),parent_import=str(Path(__file__).resolve()),worker_threads=1),
        model=json.loads((Path(model)/'manifest.json').read_text())['model_version'],source_sha256=digest,source_start_ns=first+int(offset*1e9),historical_seconds=seconds,warmup_seconds=offset,repeats=repeats,preload_seconds=preload_seconds,
        metric_definition='same heartbeat lateness as v3; causal last-symbol-input data age; separate legacy data metric preserved; explicit bounded producer queue',
        inference_pacing='each actual evaluation; bounded latest mailbox; no ordinary trading await',
        latency_outcome_scope='adapter includes abstentions/rejections; terminal also includes worker errors; actual proposal measured only when emitted',runs=[])
    with frozen_archive() as isolation:
        report['archive_gc_isolation']=isolation
        for speed in (1,4):
            for repeat in range(repeats):
                pair={}
                for shadow in ((False,True) if repeat%2==0 else (True,False)):
                    name=f'{speed}-{repeat}-{int(shadow)}'
                    result=await measure(rows,manifest,output/(name+'.jsonl'),model,shadow=shadow,speed=speed,start_ns=first+int(offset*1e9))
                    result['repeat']=repeat;pair[result['mode']]=result;report['runs'].append(result)
                    print(json.dumps({k:result[k] for k in ('mode','speed','repeat','events','forecasts','duration_seconds','absolute_event_loop_pass','adapter_budget_pass')}),flush=True)
                    (output/'latency_benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
                off,shadow=pair['off'],pair['shadow']
                shadow['ordinary_portfolio_identical']=off['portfolio_sha256']==shadow['portfolio_sha256']
                shadow['ordinary_decisions_identical']=off['ordinary_decisions_sha256']==shadow['ordinary_decisions_sha256']
                shadow['added_event_loop_p99_ms']=shadow['metrics']['event_loop_lateness_ms']['p99']-off['metrics']['event_loop_lateness_ms']['p99']
                shadow['added_budget_pass']=shadow['added_event_loop_p99_ms']<=5
                (output/'latency_benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
    after=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    report['archive_hash_after']=after;assert after==digest,'archive was mutated'
    (output/'latency_benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('model');p.add_argument('output')
    p.add_argument('--seconds',type=int,default=60);p.add_argument('--repeats',type=int,default=3);p.add_argument('--offset',type=int,default=120);a=p.parse_args()
    asyncio.run(main(a.source,a.model,a.output,a.seconds,a.repeats,a.offset))
