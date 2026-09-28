"""Strict native asyncio dispatcher for explicitly bound v5 operations.

This is a contract executor, not yet a whole-TradingEngine scheduler adapter.
Measured replay dispatch cost is never relabelled as original loop lateness.
"""
from __future__ import annotations

import asyncio
import time

from .manifest_validation import fingerprint
from .native_v5 import MODULES, NativeTapeError, validate_events
from .pipeline_evidence import quantiles

VARIANTS = {letter: frozenset(MODULES[:i+1]) for i, letter in enumerate("ABCDEF")}


class _Coordinator:
    def __init__(self, rows):
        self.rows, self.index = rows, 0
        self.changed = asyncio.Condition()
        self.failure = None
        self.outputs = []
        self.callback_ms = []
        self.ipc_receipts = []

    async def consume(self, task, kind, data=None, *, clock_method=None, producer=None):
        async with self.changed:
            while True:
                if self.failure: raise self.failure
                if self.index >= len(self.rows):
                    raise NativeTapeError(f"extra {kind} after tape end: {task}")
                row = self.rows[self.index]
                if row["task_id"] == task and (producer is None or row["producer"] == producer):
                    if row["kind"] != kind:
                        raise NativeTapeError(f"first divergence token {row['sequence']}: expected {row['kind']}, actual {kind}, task {task}")
                    if clock_method is not None:
                        if row["data"]["method"] != clock_method:
                            raise NativeTapeError(f"owned clock method mismatch at token {row['sequence']}")
                    elif fingerprint(row["data"]) != fingerprint(data):
                        raise NativeTapeError(f"dispatch/output mismatch at token {row['sequence']}: {task}/{kind}")
                    self.index += 1
                    self.changed.notify_all()
                    return row["data"].get("value") if clock_method is not None else None
                await self.changed.wait()


class NativeReplayContext:
    def __init__(self, coordinator, row):
        self.coordinator = coordinator
        self.task_id = row["task_id"]
        self.module_id = row["module_id"]
        self.source = row["source"]
        self.parent_task_id = row["parent_task_id"]
        self.producer = row["producer"]
        self.terminal_reason = "completed"

    async def clock(self, method):
        return await self.coordinator.consume(self.task_id, "clock", clock_method=method, producer=self.producer)

    async def boundary(self, name, value=None):
        await self.coordinator.consume(self.task_id, "boundary", dict(name=name, value={} if value is None else value), producer=self.producer)

    def runtime_clock(self):
        """Synchronous clocks only within a recorded uninterrupted callback slice.

        Encountering another task fails instead of blocking the asyncio thread
        or stealing that task's clocks. Native await boundaries use clock().
        """
        context = self
        class Clock:
            def _read(self, method):
                c = context.coordinator
                if c.index >= len(c.rows): raise NativeTapeError("extra synchronous clock")
                row = c.rows[c.index]
                if (row["task_id"] != context.task_id or row["kind"] != "clock"
                        or row["data"]["method"] != method):
                    raise NativeTapeError(f"synchronous callback crossed task/clock boundary at {row['sequence']}")
                c.index += 1
                return row["data"]["value"]
            def time(self): return self._read("time")
            def time_ns(self): return self._read("time_ns")
            def monotonic(self): return self._read("monotonic")
            def perf_counter_ns(self): return self._read("perf_counter_ns")
        return Clock()


class NativeControlledDriver:
    """Each call creates a fresh runtime via caller-supplied actual handlers.

    Handlers execute their operation and consume its recorded clocks/boundaries.
    The driver does not return supplied task_end outputs as computed results.
    All rows are validated before named disabled-module tasks are projected out.
    Projection cannot exclude a parent of an enabled task.
    """
    def __init__(self, tape, handlers, *, timeout_seconds=10):
        self.tape, self.handlers = tape, dict(handlers)
        if not 0 < timeout_seconds <= 60: raise ValueError("bounded replay timeout required")
        self.timeout_seconds = timeout_seconds

    async def run(self, variant):
        if variant not in VARIANTS: raise NativeTapeError("unknown A-F variant")
        validate_events(self.tape.events, self.tape.header["capture_id"])
        enabled = VARIANTS[variant]
        declarations = {r["task_id"]: r for r in self.tape.events if r["kind"] == "task_open"}
        included = {key for key, row in declarations.items() if row["module_id"] in enabled}
        for key in included:
            row = declarations[key]
            if row["parent_task_id"] is not None and row["parent_task_id"] not in included:
                raise NativeTapeError("enabled task depends on disabled module parent")
            if (row["module_id"], row["data"]["operation"]) not in self.handlers:
                raise NativeTapeError("missing native handler: "+row["module_id"]+"/"+row["data"]["operation"])
        rows = [r for r in self.tape.events if r["task_id"] in included]
        c = _Coordinator(rows)
        tasks = []

        async def execute(row):
            ctx = NativeReplayContext(c, row)
            try:
                await c.consume(ctx.task_id, "task_start", {})
                before = time.perf_counter_ns()
                outputs = await self.handlers[(ctx.module_id, row["data"]["operation"])](ctx, row["data"]["inputs"])
                if outputs is not None and not isinstance(outputs, dict):
                    raise NativeTapeError("handler must compute a dictionary output")
                elapsed = (time.perf_counter_ns()-before)/1e6
                await c.consume(ctx.task_id, "task_end", dict(reason=ctx.terminal_reason, outputs=outputs or {}))
                c.outputs.append(dict(task=ctx.task_id, module=ctx.module_id, outputs=outputs or {}))
                c.callback_ms.append(elapsed)
            except BaseException as exc:
                async with c.changed:
                    c.failure = c.failure or exc
                    c.changed.notify_all()
                raise

        async def dispatch():
            async with c.changed:
                while c.index < len(rows):
                    if c.failure: raise c.failure
                    row = rows[c.index]
                    if row["kind"] == "task_open":
                        c.index += 1
                        tasks.append(asyncio.create_task(execute(row), name="v5:"+row["task_id"]))
                        c.changed.notify_all()
                    else:
                        await c.changed.wait()
            await asyncio.gather(*tasks)

        try:
            await asyncio.wait_for(dispatch(), self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise NativeTapeError(f"native dispatcher blocked; first unconsumed token index {c.index}") from exc
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if c.index != len(rows): raise NativeTapeError("leftover dispatch/clock tokens")
        excluded = [r for r in self.tape.events if r["task_id"] not in included]
        ordinary = [r for r in c.outputs if r["module"] == "core"]
        return dict(schema="native-controlled-v5-report", variant=variant,
            populationSha256=self.tape.population_hash, provenance=self.tape.provenance,
            semanticReplay="MET", consumed=len(rows), excludedOwnedTokens=len(excluded),
            excludedModules=sorted(set(MODULES)-enabled), leftovers=0,
            accountingComplete=len(rows)+len(excluded) == len(self.tape.events),
            outputSha256=fingerprint(c.outputs), ordinaryOperationSha256=fingerprint(ordinary),
            callbackReplayMs=quantiles(c.callback_ms),
            timingScope="native asyncio replay handler residence INCLUDING token coordination; not native loop lateness or production incremental cost",
            nativeLoopMs=None, dataToAdapterMs=None, realReplayIPC=bool(c.ipc_receipts),
            replayIPCReceipts=c.ipc_receipts,
            ordinaryDecisionSha256=None, portfolioSha256=None, admissionSequenceSha256=None,
            productionCoverage=False, controlledW20="NOT_MET")
