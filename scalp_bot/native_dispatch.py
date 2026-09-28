"""Opt-in ownership of actual asyncio execution slices, independent of v4 rows.

The coroutine iterator records real suspensions/resumptions, including awaits
inside libraries. It never schedules by timestamps and does not install a global
task factory. Unregistered tasks cannot borrow a parent's ContextVar clock.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import types

from .native_v5 import NativeTapeError, OwnedClock
from .runtime_clock import SystemRuntimeClock


@dataclass(frozen=True)
class IngressIdentity:
    capture_id: str
    symbol: str
    epoch: int
    source_sequence: int
    event_id: str


class IngressRegistry:
    """Counters belong to source lanes, never to clock/research row populations."""
    def __init__(self, capture_id):
        self.capture_id = capture_id
        self.serial = Counter()
        self.latest = {}

    def accept(self, lane, symbol, *, epoch=0, event_id=None):
        if not lane or not symbol or type(epoch) is not int or epoch < 0:
            raise NativeTapeError("invalid ingress owner")
        self.serial[lane] += 1
        identity = IngressIdentity(self.capture_id, symbol, epoch, self.serial[lane],
            event_id or f"{lane}:{self.serial[lane]}")
        self.latest[lane, symbol] = identity
        return identity


@dataclass
class _Owner:
    native: object
    execution: object
    active: bool = True


def _execution():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class NativeDispatch:
    def __init__(self, writer, *, clock=None):
        self.writer = writer
        self.ingress = IngressRegistry(writer.header["capture_id"])
        self.source_clock = clock if clock is not None else SystemRuntimeClock()
        self.current = ContextVar(f"native_owner_{id(self)}", default=None)
        self.serial = Counter()
        self.tasks = {}
        self.identifiers = Counter()
        self.failure = None
        self.task_failures = []

    def fail(self, message):
        self.failure = self.failure or message
        self.writer.ring.state[4] = self.writer.ring.state[4] or 8
        raise NativeTapeError(message)

    def owner(self, *, optional=False):
        owner = self.current.get()
        if owner is None and optional:
            return None
        if owner is None or not owner.active or owner.execution is not _execution():
            self.fail("unknown or inherited native task/clock ownership")
        return owner.native

    def clock(self):
        dispatch = self
        class Clock:
            def time_ns(self): return OwnedClock(dispatch.owner(), dispatch.source_clock).time_ns()
            def time(self): return OwnedClock(dispatch.owner(), dispatch.source_clock).time()
            def monotonic(self): return OwnedClock(dispatch.owner(), dispatch.source_clock).monotonic()
            def perf_counter_ns(self): return OwnedClock(dispatch.owner(), dispatch.source_clock).perf_counter_ns()
        return Clock()

    def _open(self, module, name, *, source=None, inputs=None, root=False):
        parent = self.owner(optional=root)
        self.serial[module] += 1
        task_id = f"runtime:{module}:{self.serial[module]}:{name}"
        return self.writer.endpoint(module, "parent-asyncio").open_task(task_id, "runtime",
            parent_task_id=parent.task_id if parent is not None else None,
            source=source if source is not None else (parent.source if parent is not None else None),
            inputs=dict(name=name, values=inputs or {}))

    @contextmanager
    def scope(self, module, name, *, source=None, inputs=None, root=False):
        native = self._open(module, name, source=source, inputs=inputs, root=root)
        owner = _Owner(native, _execution())
        token = self.current.set(owner)
        native.start()
        reason = "completed"
        error = None
        try:
            yield native
        except asyncio.CancelledError as exc:
            reason = "cancelled"
            error = exc
            raise
        except BaseException as exc:
            reason = "failed"
            error = exc
            raise
        finally:
            owner.active = False
            self.current.reset(token)
            self._terminal(native, reason, error)

    def _terminal(self, native, reason, error=None):
        try:
            native.end(reason=reason)
        except BaseException:
            if error is None:
                raise
            # A poisoned writer cannot publish cleanup. Retain the original
            # failure and the invalid raw tape instead of replacing diagnostics.

    def create_task(self, coroutine, *, name, module="core", source=None, root=False):
        try:
            native = self._open(module, name, source=source, root=root)
        except BaseException:
            coroutine.close()
            raise
        state = dict(started=False, ended=False)
        dispatch = self

        @types.coroutine
        def stepped():
            owner = _Owner(native, _execution())
            token = dispatch.current.set(owner)
            state["started"] = True
            iterator = coroutine.__await__()
            value, error, serial = None, None, 0
            reason = "completed"
            failure = None
            try:
                native.start()
                while True:
                    native.boundary("dispatch_resume", dict(index=serial,
                        outcome=type(error).__name__ if error is not None else "ready"))
                    try:
                        yielded = iterator.throw(error) if error is not None else iterator.send(value)
                    except StopIteration as done:
                        return done.value
                    native.boundary("dispatch_suspend", dict(index=serial))
                    serial += 1
                    try:
                        value = yield yielded
                        error = None
                    except BaseException as exc:
                        error, value = exc, None
            except asyncio.CancelledError as exc:
                reason = "cancelled"
                failure = exc
                raise
            except BaseException as exc:
                reason = "failed"
                failure = exc
                raise
            finally:
                try:
                    # A dispatch observation can fail between iterator sends.
                    # Close its suspended frame while task ownership is valid;
                    # GC must never execute its cleanup under a later task.
                    iterator.close()
                except BaseException:
                    if failure is None:
                        raise
                finally:
                    owner.active = False
                    dispatch.current.reset(token)
                    state["ended"] = True
                    dispatch._terminal(native, reason, failure)

        async def run():
            return await stepped()

        task = self._schedule(run(), name=name)
        self.tasks[task] = native

        def completed(finished):
            # asyncio can cancel a Task without entering its coroutine at all.
            if not state["started"]:
                coroutine.close()
                state["ended"] = True
                native.end(reason="cancelled" if finished.cancelled() else "failed")
            if not state["ended"]:
                dispatch.fail("native task missing terminal")
            self.tasks.pop(finished, None)
            if not finished.cancelled() and finished.exception() is not None:
                # Keep the first failure only; completed task/coroutine frames
                # must not accumulate with every market socket receive.
                if isinstance(finished.exception(), NativeTapeError) and not self.task_failures:
                    self.task_failures.append(finished.exception())
        task.add_done_callback(lambda finished: self._callback(completed, finished))
        return task

    def _schedule(self, coroutine, *, name):
        return asyncio.create_task(coroutine, name=name)

    def _callback(self, callback, finished):
        callback(finished)

    async def run(self, coroutine, *, name="session", module="core"):
        return await self.create_task(coroutine, name=name, module=module, root=True)

    def add_done_callback(self, task, callback, *, name):
        parent = self.tasks.get(task)
        if parent is None:
            self.fail("callback registered for unknown native task")
        module = parent.endpoint.module_id
        self.serial[module] += 1
        native = self.writer.endpoint(module, "parent-asyncio").open_task(
            f"runtime:{module}:{self.serial[module]}:{name}", "runtime",
            parent_task_id=parent.task_id, source=parent.source, inputs=dict(name=name, values={}))
        def completed(finished):
            owner = _Owner(native, _execution())
            token = self.current.set(owner)
            reason = "completed"
            try:
                native.start()
                callback(finished)
            except BaseException as exc:
                reason = "failed"
                if not self.task_failures:
                    self.task_failures.append(exc)
                raise
            finally:
                owner.active = False
                self.current.reset(token)
                native.end(reason=reason)
        task.add_done_callback(lambda finished: self._callback(completed, finished))

    def boundary(self, name, value=None):
        return self.owner().boundary(name, value)

    def identifier(self, name):
        """Capture-bound metadata identity, independent of disabled modules."""
        from .manifest_validation import fingerprint
        owner = self.owner()
        key = owner.endpoint.module_id, name
        self.identifiers[key] += 1
        value = fingerprint(dict(capture=self.ingress.capture_id, module=key[0],
            name=name, ordinal=self.identifiers[key]))[:32]
        self.boundary('native_identity', dict(name=name, value=value))
        return value

    def accept_ingress(self, lane, symbol, *, epoch=0, event_id=None, payload=None):
        identity = self.ingress.accept(lane, symbol, epoch=epoch, event_id=event_id)
        self.boundary("source_ingress", dict(lane=lane, identity=asdict(identity), payload=payload or {}))
        return identity

    def source(self, symbol, lane="bybit"):
        identity = self.ingress.latest.get((lane, symbol))
        return asdict(identity) if identity is not None else None

    async def join(self):
        pending = [task for task in self.tasks if not task.done() and task is not _execution()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Let pre-start cancellation callbacks publish their terminals.
        await asyncio.sleep(0)
        if self.failure:
            raise NativeTapeError(self.failure)
        if self.task_failures:
            raise self.task_failures[0]
        for task in self.tasks:
            if task is _execution():
                continue
            if not task.done():
                self.fail("unjoined native task")
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()


def create_task(dispatch, coroutine, *, name, module="core"):
    if dispatch is None:
        return asyncio.create_task(coroutine, name=name)
    return dispatch.create_task(coroutine, name=name, module=module)
