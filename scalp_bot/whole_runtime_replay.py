"""Owned whole-coroutine replay, including synchronous cross-producer slices.

The optional greenlet adapter parks a Python stack at a native observation and
awaits its exact dispatch turn in the *same* asyncio task. No event-loop thread
waits on an IPC clock. Ordinary asyncio futures, cancellation and source adapters
remain real. This executor is not by itself evidence of production coverage.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import copy_context
from dataclasses import dataclass
from types import coroutine, SimpleNamespace

from .manifest_validation import fingerprint
from .native_controlled import VARIANTS
from .native_dispatch import NativeDispatch
from .native_v5 import MODULES, NativeEndpoint, NativeTapeError, validate_events


@dataclass
class _Suspension:
    value: object


@coroutine
def _stackful(awaitable, coordinator):
    # Replay-only import: live recording has no stack switching dependency.
    from greenlet import getcurrent, greenlet
    host = getcurrent()

    def execute():
        iterator = awaitable.__await__()
        value, error = None, None
        try:
            while True:
                try:
                    yielded = iterator.throw(error) if error is not None else iterator.send(value)
                except StopIteration as done:
                    return done.value
                try:
                    value = host.switch(_Suspension(yielded))
                    error = None
                except BaseException as exc:
                    error, value = exc, None
        finally:
            iterator.close()

    stack = greenlet(execute, parent=host)
    stack.gr_context = copy_context()
    coordinator.stacks.add(stack)
    try:
        result = stack.switch()
        while not stack.dead:
            if not isinstance(result, _Suspension):
                raise NativeTapeError('unsupported replay stack suspension')
            try:
                value = yield result.value
            except BaseException as exc:
                result = stack.throw(exc)
            else:
                result = stack.switch(value)
        return result
    finally:
        coordinator.stacks.discard(stack)
        if not stack.dead:
            stack.throw(NativeTapeError('replay stack abandoned'))


class _ReplayRing:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.state = [0]*7

    def put(self, payload, observation=None):
        return self.coordinator.observe(payload, observation[0] if observation is not None else None)


class _ReplayWriter:
    def __init__(self, coordinator):
        self.header = coordinator.tape.header
        self.ring = _ReplayRing(coordinator)

    def endpoint(self, module, producer):
        ring = self.ring.coordinator.external_endpoints.get((module, producer), self.ring)
        return NativeEndpoint(ring, module, producer)


class _ReplayDispatch(NativeDispatch):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.callbacks = set()
        super().__init__(_ReplayWriter(coordinator))

    def _schedule(self, awaitable, *, name):
        started = False
        async def run():
            nonlocal started
            started = True
            return await _stackful(awaitable, self.coordinator)
        task = asyncio.create_task(run(), name=name)
        def closed(done):
            if not started:
                awaitable.close()
        task.add_done_callback(closed)
        return task

    def _callback(self, callback, finished):
        async def run():
            callback(finished)
        task = self._schedule(run(), name='v5:completion')
        self.callbacks.add(task)
        def completed(done):
            self.callbacks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self.coordinator.poison(done.exception())
        task.add_done_callback(completed)

    async def join(self):
        await super().join()
        while self.callbacks:
            batch = list(self.callbacks)
            await asyncio.gather(*batch)
            self.callbacks.difference_update(batch)


class WholeRuntimeReplayCoordinator:
    """Run the same coroutine factory, not a list of manually invoked callbacks.

    Every attempted observation verifies declaration/parent/source/producer and
    the next exact token. A different owner parks; the same owner diverging is
    an immediate failure. Unknown roots and impossible resumes fail closed.
    Disabled hooks must actually be disabled by the factory's variant config.
    """
    def __init__(self, tape, *, variant='F', timeout_seconds=30):
        if variant not in VARIANTS:
            raise NativeTapeError('unknown A-F variant')
        if not 0 < timeout_seconds <= 60:
            raise ValueError('bounded replay timeout required')
        validate_events(tape.events, tape.header['capture_id'])
        self.tape, self.variant, self.timeout_seconds = tape, variant, timeout_seconds
        self.enabled = VARIANTS[variant]
        self.declarations = {r['task_id']: r for r in tape.events if r['kind'] == 'task_open'}
        for row in self.declarations.values():
            parent = row['parent_task_id']
            if row['module_id'] in self.enabled and parent is not None and self.declarations[parent]['module_id'] not in self.enabled:
                raise NativeTapeError('enabled task depends on disabled module parent')
        # Keep the source population streaming (IndexedNativeTape stays on disk).
        self.rows = tape.events
        self.iterator = iter(tape.events)
        self.index, self.excluded, self.consumed = 0, 0, 0
        self.current = None
        self.waiters = {}
        self.stacks = set()
        self.failure = None
        self.ipc_receipts = []
        self.counts = Counter()
        self.external_endpoints = {}
        self.dispatch = _ReplayDispatch(self)
        self.started = False
        self._advance()

    def _advance(self):
        self.current = None
        for row in self.iterator:
            if row['module_id'] in self.enabled:
                self.current = row
                break
            self.excluded += 1
            self.index += 1
        self._notify()

    @staticmethod
    def _actor(row):
        return row['task_id'], row['producer']

    def _notify(self):
        actor = self._actor(self.current) if self.current is not None else None
        for key, future in list(self.waiters.items()):
            if self.failure or actor is None or key == actor:
                if not future.done():
                    future.set_result(None)

    def poison(self, error):
        self.failure = self.failure or error
        self._notify()
        return self.failure

    def _validate_owner(self, payload):
        module, producer, task, parent, source, kind, data = payload
        declared = self.declarations.get(task)
        if declared is None:
            raise NativeTapeError(f'unknown replay owner: {task}')
        if module not in self.enabled:
            raise NativeTapeError(f'disabled module executed: {module}')
        if (module, parent, source) != (declared['module_id'], declared['parent_task_id'], declared['source']):
            raise NativeTapeError(f'replay parent/module/source mismatch: {task}')
        producers = {declared['producer']}
        if declared['data']['operation'] == 'request':
            producers.update(('inference-worker', 'reply-relay'))
        if producer not in producers:
            raise NativeTapeError(f'unknown replay producer: {producer}')
        return task, producer

    def _consume(self, payload, method):
        if self.failure:
            raise self.failure
        actor = self._validate_owner(payload)
        row = self.current
        if row is None:
            raise NativeTapeError(f'extra observation after tape end: {actor}')
        if self._actor(row) != actor:
            return None
        kind, data = payload[-2:]
        if kind != row['kind']:
            raise NativeTapeError(f'first divergence token {row["sequence"]}: {actor}, expected {row["kind"]}, actual {kind}')
        if method is not None:
            if kind != 'clock' or row['data']['method'] != method:
                raise NativeTapeError(f'owned clock method mismatch at token {row["sequence"]}: {actor}')
            value = row['data']['value']
        else:
            if fingerprint(data) != fingerprint(row['data']):
                raise NativeTapeError(f'owned observation mismatch at token {row["sequence"]}: {actor}/{kind}; expected {str(row["data"])[:1200]}, actual {str(data)[:1200]}')
            value = None
        self.index += 1
        self.consumed += 1
        self.counts[kind] += 1
        result = row['sequence'], value
        self._advance()
        return result

    async def _turn(self, actor):
        if actor in self.waiters:
            raise NativeTapeError(f'duplicate pending actor observation: {actor}')
        future = asyncio.get_running_loop().create_future()
        self.waiters[actor] = future
        self._notify()
        try:
            await future
        finally:
            self.waiters.pop(actor, None)

    def observe(self, payload, method=None):
        from greenlet import getcurrent
        try:
            while True:
                result = self._consume(payload, method)
                if result is not None:
                    return result
                stack = getcurrent()
                if stack not in self.stacks:
                    raise NativeTapeError('cross-actor observation outside a replay suspension boundary')
                self.suspend(self._turn((payload[2], payload[1])))
        except BaseException as exc:
            raise self.poison(exc)

    def suspend(self, awaitable):
        """Explicit IO/join boundary; never move the runtime to another thread."""
        from greenlet import getcurrent
        stack = getcurrent()
        if stack not in self.stacks:
            awaitable.close()
            raise NativeTapeError('IO wait outside replay suspension boundary')
        iterator = awaitable.__await__()
        value, error = None, None
        try:
            while True:
                try:
                    yielded = iterator.throw(error) if error is not None else iterator.send(value)
                except StopIteration as done:
                    return done.value
                try:
                    value = stack.parent.switch(_Suspension(yielded))
                    error = None
                except BaseException as exc:
                    value, error = None, exc
        finally:
            iterator.close()

    def worker_input(self, native, name):
        """Replay only recorded nondeterministic IPC availability, never outputs."""
        if name not in ('worker_reply_available', 'worker_process_state'):
            raise NativeTapeError('unsupported worker control input')
        actor = native.task_id, native.endpoint.producer
        try:
            while self.current is not None and self._actor(self.current) != actor:
                self.suspend(self._turn(actor))
            row = self.current
            if row is None or row['kind'] != 'boundary' or row['data']['name'] != name:
                raise NativeTapeError(f'missing worker control input: {name}')
            value = row['data']['value']
            native.boundary(name, value)
            return value
        except BaseException as exc:
            raise self.poison(exc)

    async def consume(self, task, kind, data=None, *, clock_method=None, producer=None):
        """Only the named remote actor uses this async IPC observation route."""
        declared = self.declarations.get(task)
        if declared is None:
            raise self.poison(NativeTapeError(f'unknown replay owner: {task}'))
        payload = [declared['module_id'], producer or declared['producer'], task,
            declared['parent_task_id'], declared['source'], kind, data]
        try:
            while True:
                result = self._consume(payload, clock_method)
                if result is not None:
                    return result[1]
                await self._turn((task, payload[1]))
        except BaseException as exc:
            raise self.poison(exc)

    async def run(self, factory, *, name='session', module='core'):
        if self.started:
            raise NativeTapeError('replay coordinator is single use')
        self.started = True
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.dispatch.run(factory(self.dispatch), name=name, module=module)
                await self.dispatch.join()
                if self.failure:
                    raise self.failure
                if self.current is not None:
                    raise NativeTapeError(f'enabled leftover token {self.current["sequence"]}: {self._actor(self.current)}')
        except TimeoutError as exc:
            row = self.current
            error = NativeTapeError(f'impossible resume; first unconsumed token {row}; waiting actors {list(self.waiters)}')
            raise self.poison(error) from exc
        except BaseException as exc:
            raise self.poison(exc)
        finally:
            pending = [t for t in (*self.dispatch.tasks, *self.dispatch.callbacks) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)
        return dict(schema='whole-runtime-v5-report', variant=self.variant,
            populationSha256=self.tape.population_hash, provenance=self.tape.provenance,
            consumed=self.consumed, counts=dict(self.counts), excludedOwnedTokens=self.excluded,
            excludedModules=sorted(set(MODULES)-self.enabled), leftovers=0,
            accountingComplete=self.consumed+self.excluded == len(self.tape.events),
            semanticReplay='MET', productionCoverage=False, controlledW20='NOT_MET',
            timingScope='owned coroutine replay; coordination time is not production incremental cost',
            replayIPCReceipts=self.ipc_receipts)
