"""Bounded, nonblocking parent interface and Windows-spawn inference process.

The child imports the predictor only. It has no engine, broker, scanner or client.
"""
from collections import OrderedDict
import json
import multiprocessing as mp
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
import time

from .contracts import ImpulseForecast


def _inference_main(model_dir, inbox, outbox, ttl_ns, native_endpoint=None):
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
            snapshot,side=task[:2]
            timing = dict(task[2]) if len(task) == 3 else None
            native = None
            clock = time
            if native_endpoint is not None:
                from ..native_v5 import OwnedClock
                native = native_endpoint.attach_task(**timing["nativeTask"])
                native.boundary("worker_receive")
                native.boundary("worker_start")
                clock = OwnedClock(native)
            started=clock.perf_counter_ns()
            probabilities=predictor.predict(snapshot,side)
            produced=clock.perf_counter_ns()
            if native is not None:
                native.boundary("prediction_complete", dict(probabilities=list(probabilities)))
            expires=snapshot.ref.available_mono_ns+ttl_ns
            if produced>=expires:
                result=("error","source_data_stale")
            else:
                forecast=ImpulseForecast(snapshot.ref,predictor.metadata["model_version"],
                    predictor.metadata["policy_version"],side,
                    predictor.metadata["plan_policy"]["horizon_seconds"]*1000,
                    produced,expires,*probabilities)
                result=("forecast",forecast,produced-started,process_stats())
            if timing is not None:
                timing.update(worker_start_ns=started, prediction_end_ns=produced)
                result = (*result, {"pipelineTiming":timing})
            if native is not None:
                # Queue.put begins feeder enqueue, not kernel wire completion.
                native.boundary("reply_ipc_enqueue")
            outbox.put(result,timeout=1)
    except Exception as exc:
        try:outbox.put(("error",type(exc).__name__+": "+str(exc)),timeout=.1)
        except Full:pass


class InferenceWorker:
    def __init__(self, model_dir, *, capacity=12, ttl_ms=1000, timeout_ms=2000,
                 startup_seconds=30, _target=_inference_main, trace=None, native_endpoint=None, clock=time):
        if not 1<=capacity<=128 or ttl_ms<=0 or timeout_ms<=0:raise ValueError("invalid worker bounds")
        self.model_dir=str(Path(model_dir).resolve())
        self.capacity=capacity;self.ttl_ns=ttl_ms*1_000_000;self.timeout_ns=timeout_ms*1_000_000
        self.startup_ns=int(startup_seconds*1e9);self.target=_target
        self.ctx=mp.get_context("spawn");self.inbox=self.ctx.Queue(maxsize=1);self.outbox=self.ctx.Queue(maxsize=1)
        self.latest=OrderedDict();self.active={};self.inflight=None;self.process=None
        self.ready=False;self.failed=None;self.started=0;self.coalesced=0;self.dropped=0;self.info={}
        self.closed=False
        self.trace = trace
        self.native_endpoint = native_endpoint
        self.clock = clock
        self.native_tasks = {}
        self.native_error = None
        self.timings = {}
        self.inflight_timing = None
        # Queue.get_nowait() on multiprocessing.Queue still enters Windows
        # named-pipe polling. Keep every OS receive off the market event loop.
        self.received=Queue(maxsize=1)
        self.receiver_stop=Event();self.receiver=None;self.receive_error=None

    def start(self):
        if self.process is not None or self.closed:raise RuntimeError("worker already started/closed")
        args = (self.model_dir,self.inbox,self.outbox,self.ttl_ns)
        if self.native_endpoint is not None:
            from ..native_v5 import NativeEndpoint
            args += (NativeEndpoint(self.native_endpoint.ring, "v2", "inference-worker"),)
        self.process=self.ctx.Process(target=self.target,args=args,
                                      name="scalp-ml-shadow",daemon=True)
        self.process.start();self.started=self.clock.perf_counter_ns()
        self.receiver=Thread(target=self._receive_replies,name='ml-reply-relay',daemon=True)
        self.receiver.start()

    def _receive_replies(self):
        try:
            self._relay_replies()
        except Exception as exc:
            self.receive_error = "reply_relay_failed:"+type(exc).__name__

    def _relay_replies(self):
        while not self.receiver_stop.is_set():
            try:item=self.outbox.get(timeout=.05)
            except Empty:continue
            except (EOFError,OSError,ValueError):
                if not self.receiver_stop.is_set():self.receive_error='worker_channel_closed'
                return
            timing = (item[-1].get("pipelineTiming") if isinstance(item[-1], dict) else None)
            native = self._native_from_timing(timing, producer="reply-relay")
            from ..native_v5 import OwnedClock
            relay_clock = OwnedClock(native) if native is not None else time
            if native is not None:
                native.boundary("reply_ipc_receive")
            if timing is not None:
                timing["relay_received_ns"] = relay_clock.perf_counter_ns()
            while not self.receiver_stop.is_set():
                try:
                    # Stamp while holding Queue's mutex, before notifying the
                    # consumer. A post-put timestamp races with parent poll.
                    with self.received.not_full:
                        if self.received._qsize() >= self.received.maxsize:
                            self.received.not_full.wait(.05)
                            continue
                        if timing is not None:
                            timing["relay_enqueued_ns"] = relay_clock.perf_counter_ns()
                            timing["relay_queue_depth"] = self.received._qsize()+1
                        if native is not None:
                            native.boundary("relay_enqueue")
                        self.received._put(item)
                        self.received.unfinished_tasks += 1
                        self.received.not_empty.notify()
                    break
                except Full:continue

    def activate(self,symbol,epoch):
        if self.active.get(symbol)!=epoch:
            self._terminal(symbol, "epoch_changed")
            self.latest.pop(symbol,None)
        self.active[symbol]=epoch

    def deactivate(self,symbol):
        self._terminal(symbol, "deactivated")
        self.active.pop(symbol,None);self.latest.pop(symbol,None)

    def _terminal(self, symbol, reason):
        timing = self.timings.pop(symbol, None)
        if timing is not None:
            if self.trace is not None:
                self.trace(dict(timing, terminal=reason, terminal_ns=self.clock.perf_counter_ns()))
            self._finish_native(timing, reason)

    def _native_from_timing(self, timing, *, producer=None):
        if self.native_endpoint is None or timing is None or "nativeTask" not in timing:
            return None
        endpoint = self.native_endpoint
        if producer is not None:
            from ..native_v5 import NativeEndpoint
            endpoint = NativeEndpoint(endpoint.ring, "v2", producer)
        return endpoint.attach_task(**timing["nativeTask"])

    def _finish_native(self, timing, reason):
        task = self._native_from_timing(timing)
        if task is not None and task.task_id in self.native_tasks:
            terminal = {"coalesced":"coalesced", "capacity_dropped":"dropped",
                "epoch_changed":"inactive", "deactivated":"inactive", "inactive_epoch":"inactive",
                "source_data_stale":"expired"}.get(reason, "shutdown" if reason.startswith("shutdown") else "failed")
            task.end(reason=terminal, outputs={"reason": reason})
            self.native_tasks.pop(task.task_id)

    def complete_native(self, task_id, outputs):
        """Caller records adapter decision before closing the delivered request."""
        task = self.native_tasks.pop(task_id)
        task.boundary("adapter_decision", outputs)
        task.end(outputs=outputs)

    def submit(self,snapshot,side, *, feature_ready_ns=None, probe_queued_ns=None, probe_queue_depth=None,
               native_task=None):
        if self.native_endpoint is not None and native_task is None:
            raise ValueError("v5 worker requires prospectively opened source-bound request")
        if native_task is not None:
            if self.native_endpoint is None:
                raise ValueError("v5 request requires shared worker sequencer")
            expected = native_task.source or {}
            if (native_task.endpoint.module_id != "v2" or native_task.endpoint.ring is not self.native_endpoint.ring
                    or expected.get("capture_id") != snapshot.ref.capture_id
                    or expected.get("symbol") != snapshot.ref.symbol
                    or expected.get("epoch") != snapshot.ref.selection_epoch
                    or expected.get("source_sequence") != snapshot.ref.source_sequence):
                raise ValueError("v5 request/source ownership mismatch")
            native_task.boundary("request_submit")
        if self.closed or self.failed or self.active.get(snapshot.ref.symbol)!=snapshot.ref.selection_epoch:
            if native_task is not None: native_task.end(reason="inactive", outputs={"reason":"submit_rejected"})
            return False
        key=snapshot.ref.symbol
        if key in self.latest:
            self._terminal(key, "coalesced")
            self.latest.pop(key);self.coalesced+=1
        if len(self.latest)>=self.capacity:
            self._terminal(next(iter(self.latest)), "capacity_dropped")
            self.latest.popitem(last=False);self.dropped+=1
        self.latest[key]=(snapshot,side)
        if self.trace is not None or native_task is not None:
            ref = snapshot.ref
            self.timings[key] = dict(identity=[ref.capture_id, ref.symbol, ref.selection_epoch,
                ref.source_sequence, side], available_ns=ref.available_mono_ns,
                submit_ns=self.clock.perf_counter_ns(), parent_queue_depth=len(self.latest))
            self.timings[key].update({k:v for k,v in dict(feature_ready_ns=feature_ready_ns,
                probe_queued_ns=probe_queued_ns,probe_queue_depth=probe_queue_depth).items() if v is not None})
            if native_task is not None:
                self.timings[key]["nativeTask"] = dict(task_id=native_task.task_id,
                    parent_task_id=native_task.parent_task_id, source=native_task.source)
                self.native_tasks[native_task.task_id] = native_task
        self._dispatch()
        return True

    def _dispatch(self):
        if not self.ready or self.inflight is not None or not self.latest or self.failed:return
        key,task=next(iter(self.latest.items()))
        timing = self.timings.get(key)
        if timing is not None:
            timing["dispatch_ns"] = self.clock.perf_counter_ns()
            task = (*task, dict(timing))
        native = self._native_from_timing(timing)
        if native is not None:
            native.boundary("request_dequeue")
            native.boundary("request_ipc_enqueue")
        try:self.inbox.put_nowait(task)
        except Full:
            if native is not None:
                # An attempted enqueue cannot be silently recorded as success
                # and retried under the same task. Invalidate this opt-in tape.
                with native.endpoint.ring.lock: native.endpoint.ring.state[4] = 5
                raise RuntimeError("v5 request IPC enqueue failed")
            return
        self.timings.pop(key, None)
        self.inflight_timing = dict(timing) if timing is not None else None
        self.latest.pop(key);self.inflight=(task[0].ref,self.clock.perf_counter_ns())

    def poll(self):
        """Never wait on a prediction; a dead/hung child becomes unavailable."""
        if self.closed:return []
        results=[];now=self.clock.perf_counter_ns()
        try:
            item=self.received.get_nowait()
        except Empty:item=None
        if item is not None:
            timing = (item[-1].get("pipelineTiming") if isinstance(item[-1], dict) else None)
            if timing is not None:
                native = self._native_from_timing(timing)
                if native is not None:
                    native.boundary("relay_dequeue")
                    native.boundary("adapter_receive")
                timing["adapter_receive_ns"] = self.clock.perf_counter_ns()
                timing["terminal"] = item[0] if item[0] == "forecast" else str(item[1])
                if item[0] == 'forecast' and self.active.get(item[1].source.symbol) != item[1].source.selection_epoch:
                    timing['terminal'] = 'inactive_epoch'
                if self.trace is not None: self.trace(dict(timing))
                if timing["terminal"] != "forecast": self._finish_native(timing, timing["terminal"])
            if item[0]=="ready":self.ready=True;self.info=item[1]
            else:
                self.inflight=None
                self.inflight_timing=None
                if item[0]=="forecast":
                    forecast=item[1]
                    if self.active.get(forecast.source.symbol)==forecast.source.selection_epoch:
                        results.append(item)
                else:results.append(item)
        if self.process is not None:
            if not self.process.is_alive() and item is None:self.failed="worker_crashed"
            elif self.receive_error:self.failed=self.receive_error
            elif not self.ready and now-self.started>self.startup_ns:self.failed="startup_timeout"
            elif self.inflight and now-self.inflight[1]>self.timeout_ns:self.failed="prediction_timeout"
        if self.failed:
            if self.inflight_timing is not None:
                if self.trace is not None:
                    self.trace(dict(self.inflight_timing,terminal=self.failed,terminal_ns=self.clock.perf_counter_ns()))
                self.inflight_timing=None
            for key in list(self.timings):self._terminal(key, self.failed)
            self.latest.clear()
            if self.process is not None and self.process.is_alive():
                self.process.terminate()  # Nonblocking; close owns bounded join/cleanup.
        else:self._dispatch()
        return results

    def close(self,timeout=1):
        if self.closed:return
        if self.inflight_timing is not None:
            if self.trace is not None:
                self.trace(dict(self.inflight_timing,terminal='shutdown_inflight',terminal_ns=self.clock.perf_counter_ns()))
            self.inflight_timing=None
        for key in list(self.timings):
            try: self._terminal(key, "shutdown_pending")
            except Exception as exc:
                # Capture may already be poisoned; teardown must still join all
                # producers and close IPC. The invalid tape cannot be admitted.
                self.native_error = type(exc).__name__+": "+str(exc)
        self.closed=True;self.latest.clear()
        if self.process is not None:
            try:self.inbox.put_nowait(None)
            except Full:pass
            self.process.join(timeout)
            if self.process.is_alive():
                self.process.terminate();self.process.join(timeout)
            if self.process.is_alive():raise RuntimeError("worker did not terminate")
        self.receiver_stop.set()
        if self.receiver is not None:
            self.receiver.join(max(.2,timeout))
            if self.receiver.is_alive():raise RuntimeError('worker reply relay did not terminate')
        # The process and relay are joined before terminal publication: no late
        # producer may append observations after this terminal.
        for task in list(self.native_tasks.values()):
            try:
                task.end(reason="failed" if self.failed else "shutdown",
                    outputs={"reason":self.failed or "shutdown_uncompleted"})
            except Exception as exc:
                self.native_error = type(exc).__name__+": "+str(exc)
        self.native_tasks.clear()
        for queue in (self.inbox,self.outbox):
            queue.cancel_join_thread();queue.close()
