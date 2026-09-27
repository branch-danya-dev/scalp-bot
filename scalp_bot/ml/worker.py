"""Bounded, nonblocking parent interface and Windows-spawn inference process.

The child imports the predictor only. It has no engine, broker, scanner or client.
"""
from collections import OrderedDict
import json
import multiprocessing as mp
import os
from pathlib import Path
from queue import Empty, Full
import time

from .contracts import ImpulseForecast


def _inference_main(model_dir, inbox, outbox, ttl_ns):
    os.environ["OMP_NUM_THREADS"]="1"
    os.environ["OPENBLAS_NUM_THREADS"]="1"
    os.environ["MKL_NUM_THREADS"]="1"
    try:
        from .learning import Predictor
        from .process_stats import process_stats
        predictor=Predictor(model_dir)
        import scalp_bot,sys
        outbox.put(("ready",dict(module=scalp_bot.__file__,pid=os.getpid(),
            exchange_imported="scalp_bot.bybit" in sys.modules,resources=process_stats())),timeout=1)
        while True:
            task=inbox.get()
            if task is None:return
            snapshot,side=task
            started=time.perf_counter_ns()
            probabilities=predictor.predict(snapshot,side)
            produced=time.perf_counter_ns()
            expires=snapshot.ref.available_mono_ns+ttl_ns
            if produced>=expires:
                result=("error","source_data_stale")
            else:
                forecast=ImpulseForecast(snapshot.ref,predictor.metadata["model_version"],
                    predictor.metadata["policy_version"],side,
                    predictor.metadata["plan_policy"]["horizon_seconds"]*1000,
                    produced,expires,*probabilities)
                result=("forecast",forecast,produced-started,process_stats())
            outbox.put(result,timeout=1)
    except Exception as exc:
        try:outbox.put(("error",type(exc).__name__+": "+str(exc)),timeout=.1)
        except Full:pass


class InferenceWorker:
    def __init__(self, model_dir, *, capacity=12, ttl_ms=1000, timeout_ms=2000,
                 startup_seconds=30, _target=_inference_main):
        if not 1<=capacity<=128 or ttl_ms<=0 or timeout_ms<=0:raise ValueError("invalid worker bounds")
        self.model_dir=str(Path(model_dir).resolve())
        self.capacity=capacity;self.ttl_ns=ttl_ms*1_000_000;self.timeout_ns=timeout_ms*1_000_000
        self.startup_ns=int(startup_seconds*1e9);self.target=_target
        self.ctx=mp.get_context("spawn");self.inbox=self.ctx.Queue(maxsize=1);self.outbox=self.ctx.Queue(maxsize=1)
        self.latest=OrderedDict();self.active={};self.inflight=None;self.process=None
        self.ready=False;self.failed=None;self.started=0;self.coalesced=0;self.dropped=0;self.info={}
        self.closed=False

    def start(self):
        if self.process is not None or self.closed:raise RuntimeError("worker already started/closed")
        self.process=self.ctx.Process(target=self.target,args=(self.model_dir,self.inbox,self.outbox,self.ttl_ns),
                                      name="scalp-ml-shadow",daemon=True)
        self.process.start();self.started=time.perf_counter_ns()

    def activate(self,symbol,epoch):
        if self.active.get(symbol)!=epoch:
            self.latest.pop(symbol,None)
        self.active[symbol]=epoch

    def deactivate(self,symbol):
        self.active.pop(symbol,None);self.latest.pop(symbol,None)

    def submit(self,snapshot,side):
        if self.closed or self.failed or self.active.get(snapshot.ref.symbol)!=snapshot.ref.selection_epoch:
            return False
        key=snapshot.ref.symbol
        if key in self.latest:
            self.latest.pop(key);self.coalesced+=1
        if len(self.latest)>=self.capacity:
            self.latest.popitem(last=False);self.dropped+=1
        self.latest[key]=(snapshot,side)
        self._dispatch()
        return True

    def _dispatch(self):
        if not self.ready or self.inflight is not None or not self.latest or self.failed:return
        key,task=next(iter(self.latest.items()))
        try:self.inbox.put_nowait(task)
        except Full:return
        self.latest.pop(key);self.inflight=(task[0].ref,time.perf_counter_ns())

    def poll(self):
        """Never wait on a prediction; a dead/hung child becomes unavailable."""
        if self.closed:return []
        results=[];now=time.perf_counter_ns()
        try:
            item=self.outbox.get_nowait()
        except Empty:item=None
        if item is not None:
            if item[0]=="ready":self.ready=True;self.info=item[1]
            else:
                self.inflight=None
                if item[0]=="forecast":
                    forecast=item[1]
                    if self.active.get(forecast.source.symbol)==forecast.source.selection_epoch:
                        results.append(item)
                else:results.append(item)
        if self.process is not None:
            if not self.process.is_alive() and item is None:self.failed="worker_crashed"
            elif not self.ready and now-self.started>self.startup_ns:self.failed="startup_timeout"
            elif self.inflight and now-self.inflight[1]>self.timeout_ns:self.failed="prediction_timeout"
        if self.failed:
            self.latest.clear()
            if self.process is not None and self.process.is_alive():
                self.process.terminate()  # Nonblocking; close owns bounded join/cleanup.
        else:self._dispatch()
        return results

    def close(self,timeout=1):
        if self.closed:return
        self.closed=True;self.latest.clear()
        if self.process is not None:
            try:self.inbox.put_nowait(None)
            except Full:pass
            self.process.join(timeout)
            if self.process.is_alive():
                self.process.terminate();self.process.join(timeout)
            if self.process.is_alive():raise RuntimeError("worker did not terminate")
        for queue in (self.inbox,self.outbox):
            queue.cancel_join_thread();queue.close()
