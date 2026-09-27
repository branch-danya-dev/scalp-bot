"""Measurement-only causal stage traces and immutable-archive GC isolation."""
from contextlib import contextmanager
import gc
import threading
import time
from .bybit import OrderBookState


@contextmanager
def frozen_archive():
    """Call before constructing engines. Runtime objects remain normally collected.

    Offline preloading retains hundreds of thousands of containers absent from
    streaming ingestion. Freeze only this pre-runtime object population; do not
    disable GC or freeze a warmed engine. Restore the caller's ordinary GC state.
    """
    if gc.get_freeze_count():
        raise RuntimeError('archive isolation requires an unfrozen process')
    gc.collect()
    gc.freeze()
    info=dict(frozen_objects=gc.get_freeze_count(),gc_enabled=gc.isenabled(),scope='preloaded archive and pre-runtime process objects; engines/workers created afterwards')
    try:yield info
    finally:gc.unfreeze()


class PipelineTrace:
    def __init__(self,engine,start_ns,speed):
        self.engine=engine;self.start_ns=start_ns;self.speed=speed;self.wall_start=None
        self.inputs=[];self.by_id={};self.evaluations=[];self.predictions={};self.pauses=[]
        self.last_source={};self.active_input=None;self.active_eval=None;self.gc_started={}
        self.restore=[];self.enabled=False

    def scheduled(self,mono):
        return self.wall_start+int((mono-self.start_ns)/self.speed)

    def delivered(self,event,delivered_ns):
        r=dict(input_id=event['sequence'],historical_mono_ns=event['processingMonoNs'],symbol=event['symbol'],kind=event['kind'],topic=event['body'].get('topic'),scheduled_ns=self.scheduled(event['processingMonoNs']),delivered_ns=delivered_ns)
        self.inputs.append(r);self.by_id[r['input_id']]=r
        return r

    def processing(self,event):
        self.active_input=self.by_id[event['sequence']]
        self.active_input['processing_start_ns']=time.perf_counter_ns()

    def begin_apply(self,event):
        if event.get('symbol'):
            self.last_source[event['symbol']]=(event['sequence'],event['processingMonoNs'])
        if self.enabled:self.active_input['state_apply_start_ns']=time.perf_counter_ns()

    def end_apply(self,event):
        if self.enabled:
            self.active_input['processing_end_ns']=time.perf_counter_ns()
            self.active_input=None

    def begin_eval(self,session,legacy_scheduled):
        source=self.last_source.get(session.symbol)
        if source is None:raise RuntimeError('evaluation lacks causal input provenance')
        r=dict(evaluation_id=len(self.evaluations)+1,symbol=session.symbol,input_id=source[0],available_ns=self.scheduled(source[1]),legacy_available_ns=legacy_scheduled,
            evaluation_start_ns=time.perf_counter_ns(),strategies=[])
        self.evaluations.append(r);self.active_eval=r
        return r

    def end_eval(self,row):
        row['evaluation_end_ns']=time.perf_counter_ns();self.active_eval=None

    def gc_callback(self,phase,info):
        key=(threading.get_ident(),info['generation'])
        if phase=='start':
            if self.enabled:self.gc_started[key]=time.perf_counter_ns()
        elif key in self.gc_started:
            self.pauses.append(dict(generation=info['generation'],thread=key[0],start_ns=self.gc_started.pop(key),end_ns=time.perf_counter_ns(),collected=info['collected']))

    def install(self):
        original=OrderBookState.apply
        def book_apply(state,message):
            result=original(state,message)
            if self.enabled and self.active_input is not None:
                self.active_input['market_state_ready_ns']=time.perf_counter_ns()
            return result
        OrderBookState.apply=book_apply
        self.restore.append(lambda:setattr(OrderBookState,'apply',original))
        build=self.engine._build_market_context
        def context(*a,**kw):
            before=time.perf_counter_ns();value=build(*a,**kw)
            if self.enabled and self.active_eval is not None:
                self.active_eval['context_start_ns']=before;self.active_eval['context_ready_ns']=time.perf_counter_ns()
            return value
        self.engine._build_market_context=context
        self.restore.append(lambda:setattr(self.engine,'_build_market_context',build))
        mark=self.engine._mark_execution_from_market
        def prefix(*a,**kw):
            if self.enabled and self.active_input is not None:
                self.active_input['market_state_ready_ns']=time.perf_counter_ns()
            return mark(*a,**kw)
        self.engine._mark_execution_from_market=prefix
        self.restore.append(lambda:setattr(self.engine,'_mark_execution_from_market',mark))
        for name,strategy in self.engine.strategies.items():
            base=strategy.evaluate
            def evaluate(*a,_base=base,_name=name,**kw):
                before=time.perf_counter_ns()
                try:return _base(*a,**kw)
                finally:
                    if self.enabled and self.active_eval is not None:
                        self.active_eval['strategies'].append(dict(owner=_name,start_ns=before,end_ns=time.perf_counter_ns()))
            strategy.evaluate=evaluate
            self.restore.append(lambda s=strategy,b=base:setattr(s,'evaluate',b))
        gc.callbacks.append(self.gc_callback)

    def close(self):
        if self.gc_callback in gc.callbacks:gc.callbacks.remove(self.gc_callback)
        for callback in reversed(self.restore):callback()

    def bind_worker(self,worker):
        base=worker._dispatch
        def dispatch():
            previous=worker.inflight;base()
            if worker.inflight is not None and worker.inflight is not previous:
                ref,when=worker.inflight
                if ref.source_sequence in self.predictions:
                    self.predictions[ref.source_sequence]['worker_sent_ns']=when
        worker._dispatch=dispatch

    def submitted(self,worker,snapshot,row):
        ident=snapshot.ref.source_sequence
        if snapshot.ref.symbol in worker.latest:
            old=worker.latest[snapshot.ref.symbol][0].ref.source_sequence
            self.predictions[old]['terminal']='coalesced'
        elif len(worker.latest)>=worker.capacity:
            old=next(iter(worker.latest.values()))[0].ref.source_sequence
            self.predictions[old]['terminal']='capacity_dropped'
        self.predictions[ident]=row|dict(prediction_id=ident,submit_ns=time.perf_counter_ns(),terminal='pending')

    def add_metrics(self,times):
        def delta(name,rows,left,right):
            times[name].extend((r[right]-r[left])/1e6 for r in rows if left in r and right in r)
        delta('generator_lateness_ms',self.inputs,'scheduled_ns','delivered_ns')
        delta('enqueue_wait_ms',self.inputs,'delivered_ns','enqueue_ns')
        delta('source_queue_wait_ms',self.inputs,'enqueue_ns','processing_start_ns')
        delta('due_callbacks_ms',self.inputs,'processing_start_ns','state_apply_start_ns')
        delta('state_apply_ms',self.inputs,'state_apply_start_ns','processing_end_ns')
        delta('market_state_update_ms',self.inputs,'state_apply_start_ns','market_state_ready_ns')
        delta('data_to_market_state_ready_ms',self.inputs,'scheduled_ns','market_state_ready_ns')
        delta('market_state_to_handler_end_ms',self.inputs,'market_state_ready_ns','processing_end_ns')
        delta('context_build_ms',self.evaluations,'context_start_ns','context_ready_ns')
        times['strategy_pure_ms'].extend((s['end_ns']-s['start_ns'])/1e6 for e in self.evaluations for s in e['strategies'])
        p=list(self.predictions.values())
        delta('parent_worker_queue_ms',p,'feature_end_ns','worker_sent_ns')
        delta('dispatch_to_predict_ms',p,'worker_sent_ns','predict_start_ns')
        delta('data_to_terminal_ms',p,'available_ns','terminal_ns')
        delta('legacy_data_to_terminal_ms',p,'legacy_available_ns','terminal_ns')
        times['source_queue_depth'].extend(r['queue_depth'] for r in self.inputs)
        times['gc_pause_ms'].extend((p['end_ns']-p['start_ns'])/1e6 for p in self.pauses)

    def public(self):
        return dict(clock_domain='all *_ns durations use real perf_counter_ns; historical_mono_ns used only for replay order',
            inputs=self.inputs,evaluations=self.evaluations,predictions=list(self.predictions.values()),gc=self.pauses,
            state_boundary='book reconstruction return or current trade prefix before execution; context construction return recorded separately',
            provenance='periodic evaluations refer to last actually applied input for that symbol, never the next incoming event; warmup source has no measured input span')
