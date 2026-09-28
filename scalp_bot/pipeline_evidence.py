"""Diagnostic correlation and GC telemetry, separate from causal decision clocks.

No sampling: every submitted diagnostic record is retained by the supplied sink.
The sink owns bounded storage/failure handling. Never use these OS timestamps as
strategy, label or replay-clock inputs.
"""
from collections import defaultdict
from contextlib import contextmanager
import gc
import sys
import threading
import time

# Explicitly installed by diagnostic capture tooling only.
market_sink = None


STAGES = {
    "receive_to_parse": ("receipt_ns", "parsed_ns"),
    "parse_to_enqueue": ("parsed_ns", "enqueue_ns"),
    "market_queue_wait": ("enqueue_ns", "callback_start_ns"),
    "callback": ("callback_start_ns", "callback_end_ns"),
    "book_to_features": ("book_ready_ns", "feature_ready_ns"),
    "feature_to_submit": ("feature_ready_ns", "submit_ns"),
    "probe_queue_wait": ("probe_queued_ns", "submit_ns"),
    "parent_queue_wait": ("submit_ns", "dispatch_ns"),
    "request_ipc": ("dispatch_ns", "worker_start_ns"),
    "prediction": ("worker_start_ns", "prediction_end_ns"),
    "reply_ipc": ("prediction_end_ns", "relay_received_ns"),
    "relay_enqueue_wait": ("relay_received_ns", "relay_enqueued_ns"),
    "relay_queue_wait": ("relay_enqueued_ns", "adapter_receive_ns"),
    "adapter": ("adapter_receive_ns", "adapter_end_ns"),
    "strategy_to_fire": ("strategy_ready_ns", "fire_ns"),
    "fire_to_order": ("fire_ns", "order_ns"),
    "data_to_adapter": ("available_ns", "adapter_end_ns"),
}


def market_record(message, callback_end_ns):
    row=dict(identity=message.event_id,topic=message.topic,terminal="callback_finished",
        market_queue_depth=message.queue_depth,callback_end_ns=callback_end_ns)
    for target,source in (("receipt_ns","receipt_mono_ns"),("parsed_ns","parsed_mono_ns"),
        ("enqueue_ns","enqueued_mono_ns"),("callback_start_ns","processor_started_mono_ns"),
        ("book_ready_ns","book_updated_mono_ns"),("feature_ready_ns","features_ready_mono_ns"),
        ("strategy_ready_ns","strategy_eval_finished_mono_ns"),("fire_ns","fire_mono_ns"),("order_ns","order_sent_mono_ns")):
        value=getattr(message,source,0)
        if value:row[target]=value
    return row


def quantiles(values):
    values = sorted(values)
    return dict(count=len(values), **{k:values[int((len(values)-1)*q)] if values else None
        for k,q in (("p50",.5),("p95",.95),("p99",.99),("max",1))})


def summarize(records):
    values, terminals = defaultdict(list), defaultdict(int)
    invalid = []
    for row in records:
        terminals[row.get("terminal", "unknown")] += 1
        for name, (left, right) in STAGES.items():
            if left in row and right in row:
                if row[right] < row[left]:
                    invalid.append(dict(identity=row.get("identity"), stage=name))
                else:
                    values[name].append((row[right]-row[left])/1e6)
        for key in ("parent_queue_depth", "relay_queue_depth", "market_queue_depth", "probe_queue_depth"):
            if key in row:
                values[key].append(row[key])
    return dict(stages={k:quantiles(values[k]) for k in STAGES},
        queueDepth={k:quantiles(v) for k,v in values.items() if k.endswith("_depth")},
        terminals=dict(terminals), invalidSpans=invalid,
        clock="OS perf_counter_ns diagnostic domain, never causal runtime clock",
        sampling=False)


class AllocationProbe:
    """GC overlap and retained-block pressure; does not disable/freeze GC.

    Net allocated blocks are explicitly not an allocation-throughput estimate.
    Optional tracemalloc is for separate profiling, never acceptance timings.
    """
    def __init__(self, emit):
        self.emit = emit
        self.active = {}
        self.started = {}

    def _gc(self, phase, info):
        key = (threading.get_ident(), info["generation"])
        if phase == "start":
            self.started[key] = (time.perf_counter_ns(), self.active.get(key[0]))
        elif key in self.started:
            start, identity = self.started.pop(key)
            end = time.perf_counter_ns()
            self.emit(dict(kind="gc", generation=key[1], thread=key[0], start_ns=start,
                end_ns=end, duration_ms=(end-start)/1e6, callback=identity,
                overlaps_callback=identity is not None, collected=info["collected"],
                uncollectable=info["uncollectable"], gc_enabled=gc.isenabled()))

    def __enter__(self):
        gc.callbacks.append(self._gc)
        return self

    def __exit__(self, *args):
        gc.callbacks.remove(self._gc)

    @contextmanager
    def callback(self, identity):
        thread = threading.get_ident()
        previous = self.active.get(thread)
        self.active[thread] = identity
        start, blocks = time.perf_counter_ns(), sys.getallocatedblocks()
        try:
            yield
        finally:
            end = time.perf_counter_ns()
            self.active[thread] = previous
            self.emit(dict(kind="allocation_pressure", identity=identity, start_ns=start, end_ns=end,
                net_allocated_blocks=sys.getallocatedblocks()-blocks, gc_counts=gc.get_count(),
                scope="retained_block_delta_not_total_allocation_rate"))
