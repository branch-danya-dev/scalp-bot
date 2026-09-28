"""Prospective owned causal tape. Opt-in; never upgrades a v4 recording.

One bounded shared byte ring assigns tokens under the same lock that publishes
the observation. The drain thread does JSON/hash/gzip; producers never sort by
timestamps or send delayed observations for retrospective token assignment.
This experimental contract does not claim full TradingEngine capture coverage.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import gzip
import json
import math
import multiprocessing as mp
from pathlib import Path
import struct
from threading import Thread
import time

import msgspec

from .manifest_validation import fingerprint, valid_digest

SCHEMA = "native-causal-v5"
MODULES = ("core", "segment", "cross_venue", "maker", "v3", "v2")
OPERATIONS = {
    "core": ("market", "callback", "initialize", "shutdown"),
    "segment": ("assess",),
    "cross_venue": ("event", "gap", "instruments"),
    "maker": ("post", "book", "trade", "gap", "shutdown"),
    "v3": ("label", "context", "gap", "shutdown"),
    "v2": ("request", "heartbeat", "shutdown"),
}
KINDS = ("task_open", "task_start", "clock", "boundary", "task_end")
TERMINALS = ("completed", "cancelled", "failed", "shutdown", "coalesced", "dropped", "expired", "inactive")
PIPELINE = ("features_ready", "probe_enqueue", "probe_dequeue", "request_submit",
    "request_dequeue", "request_ipc_enqueue", "worker_receive", "worker_start",
    "prediction_complete", "reply_ipc_enqueue", "reply_ipc_receive", "relay_enqueue",
    "relay_dequeue", "adapter_receive", "adapter_decision")
EXTERNAL_PIPELINE = ("enqueue", "dequeue", "dispatch")
MAX_BYTES = 32 * 1024**2
FRAME = struct.Struct("<QQI")  # token, local diagnostic monotonic ns, payload bytes


class NativeTapeError(ValueError):
    pass


class _BoundedLock:
    """A killed process must not strand the recorder forever holding its lock."""
    def __init__(self, context, state):
        self.lock = context.Lock()
        self.state = state
    def __enter__(self):
        if not self.lock.acquire(timeout=.25):
            # Emergency invalidation, never a recovered/continued recording.
            self.state[4] = 7
            raise NativeTapeError("v5 sequencer lock stalled or owner lost; capture invalid")
        return self
    def __exit__(self, *args): self.lock.release()


def schema_hash():
    return fingerprint(dict(schema=SCHEMA, modules=MODULES, operations=OPERATIONS,
        kinds=KINDS, terminals=TERMINALS, pipeline=PIPELINE, externalPipeline=EXTERNAL_PIPELINE,
        eventFields="schema sequence observed_ns module_id producer task_id parent_task_id source kind data previousHash hash",
        clockMethods=("time", "monotonic", "perf_counter_ns"),
        sourceFields="capture_id symbol epoch source_sequence event_id",
        ordering="shared-lock-at-observation", compatibility="v4-separate-no-migration"))


def provenance(*, source_sha256, config, runtime):
    if not valid_digest(source_sha256):
        raise NativeTapeError("invalid source provenance")
    return dict(sourceSha256=source_sha256, configSha256=fingerprint(config),
        runtimeSha256=fingerprint(runtime), schemaSha256=schema_hash())


def _check_provenance(value):
    if (not isinstance(value, dict) or set(value) != {
            "sourceSha256", "configSha256", "runtimeSha256", "schemaSha256"}
            or not all(valid_digest(v) for v in value.values())
            or value["schemaSha256"] != schema_hash()):
        raise NativeTapeError("invalid v5 provenance")


def _json_value(value):
    if value is None or type(value) in (str, int, bool): return
    if type(value) is float and math.isfinite(value): return
    if type(value) in (list, tuple):
        for v in value: _json_value(v)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for v in value.values(): _json_value(v)
        return
    raise NativeTapeError("non-finite or non-JSON tape value")


class _Ring:
    def __init__(self, capacity):
        if type(capacity) is not int or not 1024 <= capacity <= MAX_BYTES:
            raise NativeTapeError("v5 ring must remain within the existing 32MiB budget")
        ctx = mp.get_context("spawn")
        self.capacity = capacity
        self.buffer = ctx.RawArray("B", capacity)
        # write offset, read offset, token, rejection count, error, closed, high water
        self.state = ctx.RawArray("Q", 7)
        self.lock = _BoundedLock(ctx, self.state)

    def _copy_in(self, offset, data):
        view = memoryview(self.buffer).cast("B")
        offset %= self.capacity
        first = min(len(data), self.capacity-offset)
        view[offset:offset+first] = data[:first]
        if first < len(data): view[:len(data)-first] = data[first:]

    def _copy_out(self, offset, length):
        view = memoryview(self.buffer).cast("B")
        offset %= self.capacity
        first = min(length, self.capacity-offset)
        return bytes(view[offset:offset+first]) + bytes(view[:length-first])

    def put(self, payload, observation=None):
        with self.lock:
            s = self.state
            if s[4] or s[5]:
                s[3] += 1
                s[4] = s[4] or 1
                raise NativeTapeError("v5 sequencer closed or poisoned")
            # Clock source is read inside the publication lock. Equal clock
            # values are legal; the sequence is authoritative, not timestamps.
            try:
                if observation is not None:
                    method, source = observation
                    value = getattr(source, method)()
                    payload[-1] = dict(method=method, value=value)
                else:
                    value = None
                _json_value(payload)
                encoded = msgspec.msgpack.encode(payload)
            except BaseException:
                s[3] += 1
                s[4] = 6
                raise
            size = FRAME.size+len(encoded)
            if size > self.capacity-(s[0]-s[1]):
                s[3] += 1
                s[4] = 2
                raise NativeTapeError("v5 recording ring full; capture invalid")
            sequence = s[2]+1
            observed = time.perf_counter_ns()
            self._copy_in(s[0], FRAME.pack(sequence, observed, len(encoded)))
            self._copy_in(s[0]+FRAME.size, encoded)
            s[0] += size
            s[2] = sequence
            s[6] = max(s[6], s[0]-s[1])
            return sequence, value

    def drain(self):
        batch = []
        with self.lock:
            s = self.state
            offset = start = s[1]
            while offset < s[0] and len(batch) < 1024 and offset-start < 4*1024**2:
                seq, stamp, size = FRAME.unpack(self._copy_out(offset, FRAME.size))
                batch.append((seq, stamp, self._copy_out(offset+FRAME.size, size)))
                offset += FRAME.size+size
            done = bool(s[5] and s[1] == s[0])
        return batch, done

    def acknowledge(self, batch):
        # One writer, one in-flight batch. Published bytes stay charged until
        # hashing/encoding/compression/write succeeds, just like InputWriter.
        if not batch: return
        with self.lock:
            seq, _, _ = FRAME.unpack(self._copy_out(self.state[1], FRAME.size))
            if seq != batch[0][0]: raise NativeTapeError("out-of-order writer acknowledgment")
            self.state[1] += sum(FRAME.size+len(row[2]) for row in batch)

    def health(self):
        with self.lock:
            s = self.state
            return dict(accepted=s[2], rejected=s[3], errorCode=s[4], closed=bool(s[5]),
                pendingBytes=s[0]-s[1], highWaterBytes=s[6], capacityBytes=self.capacity)


@dataclass(frozen=True)
class NativeEndpoint:
    ring: _Ring
    module_id: str
    producer: str

    def open_task(self, task_id, operation, *, parent_task_id=None, source=None, inputs=None):
        task = NativeTask(self, task_id, parent_task_id, source)
        task.emit("task_open", dict(operation=operation, inputs={} if inputs is None else inputs))
        return task

    def attach_task(self, task_id, *, parent_task_id=None, source=None):
        """Transfer an existing task to a declared thread/process, not a new task."""
        return NativeTask(self, task_id, parent_task_id, source)


@dataclass(frozen=True)
class NativeTask:
    endpoint: NativeEndpoint
    task_id: str
    parent_task_id: str | None
    source: dict | None

    def emit(self, kind, data, *, observation=None):
        return self.endpoint.ring.put([self.endpoint.module_id, self.endpoint.producer,
            self.task_id, self.parent_task_id, self.source, kind, data], observation)[0]

    def start(self): return self.emit("task_start", {})
    def boundary(self, name, value=None): return self.emit("boundary", dict(name=name, value={} if value is None else value))
    def end(self, *, reason="completed", outputs=None):
        return self.emit("task_end", dict(reason=reason, outputs={} if outputs is None else outputs))


class OwnedClock:
    def __init__(self, task, source=time):
        self.task, self.source = task, source

    def _read(self, method):
        t = self.task
        return t.endpoint.ring.put([t.endpoint.module_id, t.endpoint.producer,
            t.task_id, t.parent_task_id, t.source, "clock", None], (method, self.source))[1]

    def time(self): return self._read("time")
    def monotonic(self): return self._read("monotonic")
    def perf_counter_ns(self): return self._read("perf_counter_ns")


def _seal(row, previous):
    row = dict(row, previousHash=previous)
    row["hash"] = fingerprint(row)
    return row


class NativeTapeWriter:
    """The writer owns the ring until every producer is joined, then close()."""
    def __init__(self, path, *, capture_id, provenance, capacity_bytes=MAX_BYTES):
        _check_provenance(provenance)
        if not isinstance(capture_id, str) or not capture_id:
            raise NativeTapeError("capture identity required")
        self.ring = _Ring(capacity_bytes)
        self.header = dict(schema=SCHEMA, kind="header", capture_id=capture_id,
            provenance=dict(provenance), coverage="explicit_owned_tasks_only",
            productionCoverage=False, ordering="shared-lock-at-observation",
            capacityBytes=capacity_bytes)
        self.path = Path(path)
        self.stream = self.path.open("xb")
        self.written = self.batches = self.max_batch = 0
        self.error = None
        self.closed = False
        self.thread = Thread(target=self._run, name="native-v5-writer", daemon=True)
        self.thread.start()

    def endpoint(self, module_id, producer):
        if module_id not in MODULES or not isinstance(producer, str) or not producer:
            raise NativeTapeError("explicit module and producer required")
        return NativeEndpoint(self.ring, module_id, producer)

    def _run(self):
        previous = None
        def write(stream, row):
            nonlocal previous
            row = _seal(row, previous)
            stream.write(msgspec.json.encode(row)+b"\n")
            previous = row["hash"]
        try:
            with self.stream, gzip.GzipFile(fileobj=self.stream, mode="wb", compresslevel=1, mtime=0) as stream:
                write(stream, self.header)
                while True:
                    batch, done = self.ring.drain()
                    if batch:
                        self.batches += 1
                        self.max_batch = max(self.max_batch, len(batch))
                    for sequence, observed_ns, encoded in batch:
                        module, producer, task, parent, source, kind, data = msgspec.msgpack.decode(encoded)
                        write(stream, dict(schema=SCHEMA, sequence=sequence, observed_ns=observed_ns,
                            module_id=module, producer=producer, task_id=task,
                            parent_task_id=parent, source=source, kind=kind, data=data))
                        self.written += 1
                    self.ring.acknowledge(batch)
                    if done: break
                    if not batch: time.sleep(.001)
                health = self.ring.health()
                write(stream, dict(schema=SCHEMA, kind="footer", count=self.written,
                    lastSequence=health["accepted"], valid=not health["errorCode"],
                    health=health))
        except BaseException as exc:
            self.error = type(exc).__name__+": "+str(exc)
            # Emergency invalidation only. A crashed producer may own the lock;
            # do not attempt to repair/publish any sequence under that condition.
            self.ring.state[4] = 3

    def health(self):
        try: health = self.ring.health()
        except NativeTapeError: health = dict(errorCode=7, rejected=None, accepted=None, pendingBytes=None)
        return dict(health, written=self.written, writerError=self.error,
            batches=self.batches, maxBatchRows=self.max_batch, writerAlive=self.thread.is_alive())

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            with self.ring.lock: self.ring.state[5] = 1
        except NativeTapeError as exc:
            self.error = self.error or str(exc)
        self.thread.join(15)
        if self.thread.is_alive():
            self.error = self.error or "native writer shutdown timeout"
            raise NativeTapeError(self.error)

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.ring.state[4] = self.ring.state[4] or 4
        self.close()


@dataclass(frozen=True)
class NativeTape:
    header: dict
    events: list
    footer: dict
    counts: dict
    population_hash: str

    @property
    def provenance(self): return self.header["provenance"]


def _source(value, capture_id):
    if value is None: return
    if (not isinstance(value, dict) or set(value) != {
            "capture_id", "symbol", "epoch", "source_sequence", "event_id"}
            or value["capture_id"] != capture_id
            or any(type(value[k]) is not int or value[k] < 0 for k in ("epoch", "source_sequence"))
            or any(not isinstance(value[k], str) or not value[k] for k in ("symbol", "event_id"))):
        raise NativeTapeError("incomplete source/capture correlation")


def validate_events(events, capture_id):
    tasks, counts = {}, Counter()
    expected = set("schema sequence observed_ns module_id producer task_id parent_task_id source kind data".split())
    for sequence, row in enumerate(events, 1):
        if set(row)-{"hash", "previousHash"} != expected:
            raise NativeTapeError("unexpected v5 event fields")
        module, task_id, kind, data = (row[k] for k in ("module_id", "task_id", "kind", "data"))
        if (row["schema"] != SCHEMA or type(row["sequence"]) is not int or row["sequence"] != sequence
                or type(row["observed_ns"]) is not int or row["observed_ns"] < 0
                or module not in MODULES or kind not in KINDS or not isinstance(data, dict)
                or not isinstance(task_id, str) or not task_id
                or not isinstance(row["producer"], str) or not row["producer"]):
            raise NativeTapeError("invalid/missing/duplicate dispatch token or owner")
        _json_value(data)
        _source(row["source"], capture_id)
        parent = row["parent_task_id"]
        if kind == "task_open":
            if (task_id in tasks or parent is not None and parent not in tasks
                    or set(data) != {"operation", "inputs"} or not isinstance(data["inputs"], dict)
                    or data["operation"] not in OPERATIONS[module]):
                raise NativeTapeError("invalid task/parent/operation declaration")
            if data["operation"] == "request" and row["source"] is None:
                raise NativeTapeError("V2 request requires exact source identity")
            tasks[task_id] = dict(module=module, parent=parent, source=row["source"],
                operation=data["operation"], state="open", stage=0)
        else:
            task = tasks.get(task_id)
            if task is None or (module, parent, row["source"]) != (task["module"], task["parent"], task["source"]):
                raise NativeTapeError("task/module/source ownership mismatch")
            if task["state"] == "ended": raise NativeTapeError("observation after task terminal")
            if kind == "task_start":
                if task["state"] != "open" or data: raise NativeTapeError("duplicate task dispatch")
                task["state"] = "started"
            elif kind == "task_end":
                if (set(data) != {"reason", "outputs"} or data["reason"] not in TERMINALS
                        or not isinstance(data["outputs"], dict)
                        or data["reason"] == "completed" and task["state"] != "started"):
                    raise NativeTapeError("invalid task terminal")
                required = PIPELINE if module == "v2" and task["operation"] == "request" else (
                    EXTERNAL_PIPELINE if module == "cross_venue" else ())
                if data["reason"] == "completed" and required and task["stage"] != len(required):
                    raise NativeTapeError("missing request/reply/relay or external boundary")
                task["state"] = "ended"
            elif task["state"] != "started":
                raise NativeTapeError("observation before task dispatch")
            elif kind == "clock":
                if set(data) != {"method", "value"}: raise NativeTapeError("invalid owned clock")
                method, value = data["method"], data["value"]
                if (method not in ("time", "monotonic", "perf_counter_ns")
                        or type(value) not in (int, float) or not math.isfinite(value)
                        or method == "perf_counter_ns" and (type(value) is not int or value < 0)):
                    raise NativeTapeError("invalid owned clock value")
            elif kind == "boundary":
                if (set(data) != {"name", "value"} or not isinstance(data["name"], str)
                        or not data["name"] or not isinstance(data["value"], dict)):
                    raise NativeTapeError("invalid dispatch boundary")
                if module == "v2" and task["operation"] == "request":
                    if task["stage"] >= len(PIPELINE) or data["name"] != PIPELINE[task["stage"]]:
                        raise NativeTapeError("request/reply/relay dispatch out of order")
                    task["stage"] += 1
                elif module == "cross_venue":
                    if task["stage"] >= len(EXTERNAL_PIPELINE) or data["name"] != EXTERNAL_PIPELINE[task["stage"]]:
                        raise NativeTapeError("external dispatch out of order")
                    task["stage"] += 1
        counts[kind] += 1
    if any(task["state"] != "ended" for task in tasks.values()):
        raise NativeTapeError("leftover unfinished task tokens")
    return dict(counts)


def read_native_tape(path, *, expected_provenance=None, max_events=1_000_000):
    """Bounded fixture reader. A long production tape needs the indexed adapter.

    max_events is an explicit fail limit, never silent truncation or sampling.
    Hash validation precedes all execution; a missing footer/suffix always fails.
    """
    rows, previous = [], None
    try:
        with gzip.open(path, "rb") as stream:
            while raw := stream.readline(MAX_BYTES+1):
                if len(raw) > MAX_BYTES: raise NativeTapeError("oversized v5 row")
                row = msgspec.json.decode(raw)
                if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                    raise NativeTapeError("unsupported version; v4 requires its original reader")
                if (row.get("previousHash") != previous or row.get("hash") != fingerprint(
                        {k:v for k,v in row.items() if k != "hash"})):
                    raise NativeTapeError("v5 canonical hash/chain mismatch")
                previous = row["hash"]
                rows.append(row)
                if len(rows) > max_events+2: raise NativeTapeError("v5 reader population bound exceeded")
    except (OSError, EOFError, msgspec.DecodeError) as exc:
        raise NativeTapeError("incomplete or corrupt v5 stream") from exc
    if len(rows) < 2 or rows[0].get("kind") != "header" or rows[-1].get("kind") != "footer":
        raise NativeTapeError("missing v5 header/footer suffix")
    header, footer = rows[0], rows[-1]
    if (set(header) != {"schema", "kind", "capture_id", "provenance", "coverage",
            "productionCoverage", "ordering", "capacityBytes", "previousHash", "hash"}
            or not isinstance(header.get("capture_id"), str) or not header["capture_id"]
            or type(header.get("capacityBytes")) is not int or not 1024 <= header["capacityBytes"] <= MAX_BYTES
            or set(footer) != {"schema", "kind", "count", "lastSequence", "valid", "health", "previousHash", "hash"}
            or type(footer.get("count")) is not int or type(footer.get("lastSequence")) is not int
            or not isinstance(footer.get("health"), dict)):
        raise NativeTapeError("invalid v5 header/footer fields")
    _check_provenance(header.get("provenance"))
    if expected_provenance is not None and header["provenance"] != expected_provenance:
        raise NativeTapeError("exact source/config/runtime/schema provenance mismatch")
    if (header.get("ordering") != "shared-lock-at-observation"
            or header.get("coverage") != "explicit_owned_tasks_only"
            or header.get("productionCoverage") is not False):
        raise NativeTapeError("unsupported v5 coverage contract")
    events = rows[1:-1]
    if (footer.get("valid") is not True or footer.get("count") != len(events)
            or footer.get("lastSequence") != len(events)
            or footer.get("health", {}).get("rejected") != 0
            or footer.get("health", {}).get("errorCode") != 0
            or footer.get("health", {}).get("pendingBytes") != 0):
        raise NativeTapeError("invalid or incomplete v5 recording")
    counts = validate_events(events, header["capture_id"])
    return NativeTape(header, events, footer, counts, fingerprint([r["hash"] for r in events]))


def write_sealed_rows(path, header, events):
    """Test/export helper: preserves tokens, reseals bytes, never validates them.

    Consumers MUST still validate lifecycle/order/provenance, not trust a chain.
    """
    previous = None
    with gzip.open(path, "xb", compresslevel=1) as stream:
        rows = [header, *events, dict(schema=SCHEMA, kind="footer", count=len(events),
            lastSequence=events[-1]["sequence"] if events else 0, valid=True,
            health=dict(rejected=0, errorCode=0, pendingBytes=0))]
        for row in rows:
            sealed = _seal({k:v for k,v in row.items() if k not in ("hash", "previousHash")}, previous)
            stream.write(msgspec.json.encode(sealed)+b"\n")
            previous = sealed["hash"]
