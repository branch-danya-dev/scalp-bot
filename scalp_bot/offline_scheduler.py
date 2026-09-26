"""Deterministic fast-task dispatch for bounded, contiguous input windows.

No asyncio task or wall-clock sleep is created. Configuration/pre-state and
full-session provenance remain the caller's responsibility.
"""
import asyncio
from contextvars import copy_context
from copy import deepcopy
from .domain import Candle
from .bybit import OrderBookSequenceError
from .source_failure import SourceFailure

from .input_journal import SCHEMA, valid_body
from .input_scope import InputScopes
from .manifest_validation import fingerprint
from .manifest_validation import check_manifest
from .offline_segment import OfflineSegmentReplay, SegmentMismatch, _Cursor, root_inputs
from .offline_transport import ReplayTransport


class _Pause:
    def __init__(self, delay):
        self.delay = delay

    def __await__(self):
        yield self


class _ContextPause:
    def __init__(self, identity):
        self.identity = identity

    def __await__(self):
        yield self


class _SourcePause(_ContextPause):
    pass


def comparable_events(events):
    result = deepcopy(events)
    for event in result:
        if event['event'] == 'run_summary':
            for key in ('latencyMetrics', 'recorderHealth'):
                event['payload'].pop(key, None)
    return result


class OfflineScheduledReplay(OfflineSegmentReplay):
    async def _pause_source(self, identity):
        await _SourcePause(identity)

    def __init__(self, engine):
        super().__init__(engine)
        self.transport = ReplayTransport(self)

    async def apply(self, records, *, expected_events=None, event_sink=None):
        # The indexed capture reader returns fresh decoded rows on every access.
        # Keep legacy in-memory callers detached; never materialize a long run.
        rows = records if getattr(records, "disk_backed", False) else deepcopy(list(records))
        allowed = {'scope', 'clock_read', 'bootstrap', 'rest_context', 'market_message',
                   'callback', 'symbol_lifecycle', 'scheduler', 'clock_sample', 'clock_error', 'external', 'dispatch', 'scanner_result', 'control', 'manifest', 'run_end', 'transport', 'service', 'footer', 'source_error', 'context_await', 'source_await'}
        previous = None
        for row in rows:
            if (row.get('schema') != SCHEMA or row.get('kind') not in allowed
                    or not valid_body(row['kind'], row.get('symbol'), row.get('body'))
                    or row.get('hash') != fingerprint({k: v for k, v in row.items() if k != 'hash'})):
                raise SegmentMismatch('unsupported or corrupt scheduler input')
            if previous and (row['previousHash'] != previous['hash']
                             or row['sequence'] != previous['sequence'] + 1):
                raise SegmentMismatch('non-contiguous scheduler window')
            previous = row
        if not rows or (rows[0]['kind'] not in ('transport', 'service') and (rows[0]['kind'] != 'scope' or rows[0]['body']['phase'] != 'begin')):
            raise SegmentMismatch('window must begin at a handler scope')
        self._admit_origin(rows)
        engine = self.engine
        if self.failed:
            raise SegmentMismatch('discard engine after previous replay failure')
        if rows[0]['sequence'] <= self.last_sequence:
            raise SegmentMismatch('window already consumed or out of order')
        if engine.input_journal is not None or engine._tasks or engine._event_tasks:
            raise SegmentMismatch('requires idle engine with capture off')
        tasks = {}
        loops = {}
        roots = []
        service = None
        service_started = False
        service_closed = False
        service_pending = set()
        loop_methods = {'clock_loop': engine._clock_loop, 'arbiter_loop': engine._arbiter_loop,
                        'scanner_loop': engine._scanner_loop, 'context_loop': engine._context_loop}
        cursor = _Cursor(rows)
        scopes = InputScopes(cursor)
        first_scope = next((r for r in rows if r['kind'] == 'scope' and r['body']['phase'] == 'begin'), None)
        scopes.serial = first_scope['body']['id'] - 1 if first_scope else 0
        cursor.scopes = scopes
        events = event_sink if event_sink is not None else []
        def launch_service():
            service_pending.update(('scanner_loop', 'context_loop', 'arbiter_loop'))
            if engine.config.exchange_clock_enabled:
                service_pending.add('clock_loop')

        async def shutdown_service():
            service_pending.clear()  # Tasks cancelled before their first instruction have no scope.
            await _Pause(0)  # Resume only at footer after all recorded cleanup.
        def trading_manifest():
            candidate = next((r['body']['manifest'] for r in rows[cursor.index:]
                              if r['kind'] == 'manifest' and r['body']['phase'] == 'run'), None)
            if candidate is None or check_manifest(candidate)[1]:
                raise SegmentMismatch('recorded run manifest unavailable')
            origin = getattr(engine, 'replay_origin', None)
            if origin is None:
                raise SegmentMismatch('Start requires cold provenance-bound engine')
            if (candidate['configSha256'] != origin['configSha256']
                    or candidate['manifestVersion'] not in (3, 4)
                    or candidate['recordSchemaVersion'] != 'jsonl-clock-v2'
                    or candidate['executionModelVersion'] != 'paper-v1'
                    or candidate['code']['sourceSha256'] != origin['sourceSha256']
                    or candidate['runtimeSha256'] != origin['runtimeSha256']
                    or candidate['researchPolicy'] != engine.research_policy.public()
                    or candidate['strategies']['enabled'] != sorted(k for k, v in engine.strategy_enabled.items() if v)):
                raise SegmentMismatch('run manifest binding mismatch')
            return deepcopy(candidate)

        def launch_timer():
            old = loops.get('paper_timer')
            if old and old['state'] not in ('returned', 'cancelled'):
                raise SegmentMismatch('overlapping paper timers unsupported')
            loops['paper_timer'] = dict(context=copy_context(), coroutine=engine._paper_run_timer(),
                state='scheduled', periodic=True, wait=None, cancel_requested=False)

        def cancel_timer():
            task = loops.get('paper_timer')
            if task is None or task['state'] in ('returned', 'cancelled'):
                return
            task['cancel_requested'] = True
            if task['state'] == 'scheduled':
                task['coroutine'].close()
                task['state'] = 'cancelled'

        def launch(symbol, reason, identity):
            if identity in tasks:
                raise SegmentMismatch('duplicate scheduled task')
            tasks[identity] = dict(symbol=symbol, context=copy_context(), started=False,
                coroutine=engine._run_event_evaluation(symbol, reason, capture_id=identity),
                state='scheduled', scope=None)
            engine.sessions[symbol].event_eval_owner = tasks[identity]

        async def sleep(delay):
            await _Pause(delay)

        async def sync_clock():
            return await self._dispatch('clock_sync', None, [])

        async def scan():
            await self._dispatch('scan', None, [])

        async def context_results(items):
            if cursor.peek()['kind'] == 'context_await':
                identity = engine._begin_context_batch(items)
                stale = []
                try:
                    for symbol, session in items:
                        await _ContextPause(identity)
                        engine._record_input('context_await', symbol, {'id': identity, 'phase': 'request'})
                        stale.append(engine._context_needs_1m(session))
                    await _ContextPause(identity)
                except asyncio.CancelledError:
                    engine._record_input('context_await', None, {'id': identity, 'phase': 'cancelled'})
                    raise
                engine._record_input('context_await', None, {'id': identity, 'phase': 'ready'})
            else:
                # Legacy windows have no observable await boundaries. Their
                # original non-interleaved execution remains supported only.
                stale = [engine._context_needs_1m(session) for _, session in items]
            selected = []
            for row in rows[cursor.index:]:
                if row['kind'] == 'dispatch':
                    break
                if row['kind'] == 'rest_context':
                    selected.append(row)
                if row['kind'] == 'source_error':
                    if row['body']['source'] != 'context' or row['symbol'] is None:
                        raise SegmentMismatch('unsupported source failure in context batch')
                    selected.append(row)
                if len(selected) == len(items):
                    break
            if len(selected) != len(items):
                raise SegmentMismatch('incomplete context batch')
            results = []
            for (symbol, _), needs_1m, row in zip(items, stale, selected, strict=True):
                body = row['body']
                if row['symbol'] != symbol:
                    raise SegmentMismatch('context result symbol mismatch')
                if row['kind'] == 'source_error':
                    results.append(SourceFailure(body['errorType']))
                    continue
                if (body['candles'] is not None) != needs_1m:
                    raise SegmentMismatch('context order or freshness branch mismatch')
                results.append(tuple(None if body[k] is None else [Candle(**c) for c in body[k]]
                    for k in ('candles', 'context5m', 'context15m', 'context1h')))
            return results

        def step(task, cancel=False):
            coroutine = task['coroutine']
            task['started'] = True
            try:
                yielded = task['context'].run(coroutine.throw, asyncio.CancelledError()) if cancel else (
                    task['context'].run(coroutine.send, None))
            except StopIteration:
                task['state'] = 'returned'
            except asyncio.CancelledError:
                task['state'] = 'cancelled'
            else:
                if isinstance(yielded, _SourcePause):
                    task['state'] = 'source_waiting'
                    task['source_wait'] = yielded.identity
                    return
                if isinstance(yielded, _ContextPause):
                    task['state'] = 'context_waiting'
                    task['context_batch'] = yielded.identity
                    return
                if not isinstance(yielded, _Pause):
                    raise SegmentMismatch('unsupported await in replay task')
                task['state'] = 'sleeping'
                if task.get('periodic'):
                    prior = rows[cursor.index - 1]
                    if prior['kind'] != 'dispatch' or prior['body']['phase'] != 'wait':
                        raise SegmentMismatch('periodic pause without wait marker')
                    task['wait'] = prior['body']['id']

        # Preserve instance overrides exactly; do not leave bound methods shadowing
        # class methods after the replay window.
        missing = object()
        saved = {key: engine.__dict__.get(key, missing) for key in
                 ('_launch_event_evaluation', '_event_evaluation_sleep', '_periodic_sleep', '_sync_clock_once', '_scan_once', '_fetch_context_results', '_build_trading_manifest', '_launch_run_timer', '_cancel_run_timer', '_launch_service_tasks', '_shutdown_service_tasks')}
        writer_override = engine.recorder.__dict__.get('start_background_writer', missing)
        try:
            with self._bindings(cursor, scopes, rows[0], events):
                engine._launch_event_evaluation = launch
                engine._event_evaluation_sleep = sleep
                engine._periodic_sleep = sleep
                engine._sync_clock_once = sync_clock
                engine._scan_once = scan
                engine._fetch_context_results = context_results
                engine._build_trading_manifest = trading_manifest
                engine._launch_run_timer = launch_timer
                engine._cancel_run_timer = cancel_timer
                engine._launch_service_tasks = launch_service
                engine._shutdown_service_tasks = shutdown_service
                engine.recorder.start_background_writer = lambda: None
                self._control_dispatch = True
                try:
                    while cursor.index < len(rows):
                        row = cursor.peek()
                        body = row['body']
                        if row['kind'] == 'service':
                            if not getattr(engine, 'replay_origin', None):
                                raise SegmentMismatch('service replay requires cold engine')
                            from .engine import TradingEngine
                            if body['phase'] == 'start':
                                if service_started:
                                    raise SegmentMismatch('duplicate service start')
                                service_started = True
                                coroutine = TradingEngine.start(engine)
                            else:
                                if not service_started or service is not None:
                                    raise SegmentMismatch('close without matching service start')
                                coroutine = engine.close()
                            task = dict(context=copy_context(), coroutine=coroutine, state='scheduled')
                            if body['phase'] == 'start':
                                roots.append(task)
                            else:
                                service = task
                            step(task)
                            if body['phase'] == 'start':
                                if task['state'] not in ('returned', 'source_waiting'):
                                    raise SegmentMismatch('service startup await is not supported')
                        elif row['kind'] == 'footer':
                            if (service is None or service['state'] != 'sleeping' or tasks
                                    or any(t['state'] not in ('returned', 'cancelled') for t in [*loops.values(), *roots])
                                    or any(phase != 'drained' for w in self.transport.workers.values()
                                           for _, phase in w['channels'].values())):
                                raise SegmentMismatch('footer before service cleanup completed')
                            step(service)
                            if service['state'] != 'returned' or cursor.index != len(rows):
                                raise SegmentMismatch('invalid service closure suffix')
                            service_closed = True
                        elif row['kind'] == 'transport':
                            self.transport.apply(row, cursor)
                        elif row['kind'] == 'context_await':
                            task = loops.get('context_loop')
                            if (task is None or task['state'] != 'context_waiting'
                                    or task['context_batch'] != body['id']
                                    or body['phase'] not in ('request', 'ready', 'cancelled')):
                                raise SegmentMismatch('unmatched context await dispatch')
                            step(task, cancel=body['phase'] == 'cancelled')
                        elif row['kind'] == 'source_await':
                            matches = [t for t in [*loops.values(), *roots]
                                if t['state'] == 'source_waiting' and t['source_wait'] == body['id']]
                            if len(matches) != 1 or body['phase'] not in ('ready', 'cancelled', 'failed'):
                                raise SegmentMismatch('unsupported or unmatched source resume')
                            step(matches[0], cancel=body['phase'] == 'cancelled')
                        elif row['kind'] == 'scope' and body['phase'] == 'begin':
                            if body['name'] == 'paper_timer':
                                task = loops.get('paper_timer')
                                if task is None or task['state'] != 'scheduled':
                                    raise SegmentMismatch('paper timer without Start')
                                step(task)
                            elif body['name'] in loop_methods:
                                if service_started:
                                    if body['name'] not in service_pending:
                                        raise SegmentMismatch('periodic task outside service launch')
                                    service_pending.remove(body['name'])
                                if body['name'] in loops:
                                    raise SegmentMismatch('duplicate periodic loop')
                                context = copy_context()
                                context.run(scopes.current.set, body['parentId'])
                                task = dict(context=context, coroutine=loop_methods[body['name']](),
                                            state='scheduled', periodic=True, wait=None)
                                loops[body['name']] = task
                                step(task)
                            elif body['name'] == 'event_evaluation':
                                following = rows[cursor.index + 1] if cursor.index + 1 < len(rows) else {}
                                if following.get('kind') != 'scheduler' or following['body']['phase'] != 'started':
                                    raise SegmentMismatch('missing task start')
                                task = tasks.get(following['body']['taskId'])
                                if task is None or task['state'] != 'scheduled':
                                    raise SegmentMismatch('unknown or repeated task start')
                                task['scope'] = body['id']
                                step(task)
                            else:
                                root = await self._root(rows, cursor, scopes)
                                if root is not None:
                                    roots.append(root)
                        elif row['kind'] == 'dispatch' and body['phase'] in ('wake', 'cancelled'):
                            matches = [t for t in loops.values() if t['state'] == 'sleeping' and t['wait'] == body['id']]
                            if len(matches) != 1:
                                raise SegmentMismatch('unknown periodic wake or cancellation')
                            if (matches[0] is loops.get('paper_timer') and body['phase'] == 'cancelled'
                                    and not matches[0]['cancel_requested']):
                                raise SegmentMismatch('paper timer cancellation without control request')
                            step(matches[0], cancel=body['phase'] == 'cancelled')
                        elif row['kind'] == 'scheduler' and body['phase'] == 'resumed':
                            task = tasks.get(body['taskId'])
                            if task is None or task['state'] != 'sleeping':
                                raise SegmentMismatch('resume without sleeping task')
                            step(task)
                        elif row['kind'] == 'scope' and body['phase'] == 'end' and body['outcome'] == 'cancelled':
                            matches = [t for t in tasks.values() if t['scope'] == body['id'] and t['state'] == 'sleeping']
                            if len(matches) != 1:
                                raise SegmentMismatch('unknown cancellation scope')
                            step(matches[0], cancel=True)
                        elif row['kind'] == 'scheduler' and body['phase'] == 'finished':
                            task = tasks.get(body['taskId'])
                            if task is None:
                                raise SegmentMismatch('completion without task')
                            if task['state'] == 'scheduled' and body['outcome'] == 'cancelled':
                                task['coroutine'].close()
                                task['state'] = 'cancelled'
                            if task['state'] not in ('returned', 'cancelled') or task['state'] != body['outcome']:
                                raise SegmentMismatch('task completion outcome mismatch')
                            engine._record_scheduler('finished', task['symbol'], body['taskId'], outcome=task['state'])
                            engine._complete_event_evaluation(task['symbol'], task)
                            del tasks[body['taskId']]
                        else:
                            raise SegmentMismatch(f'unsupported dispatch at sequence {row["sequence"]}')
                    if (tasks or any(t['state'] not in ('returned', 'cancelled') for t in [*loops.values(), *roots])
                            or service_started and not service_closed):
                        raise SegmentMismatch('window ends with unfinished tasks')
                    if expected_events is not None and comparable_events(events) != comparable_events(expected_events):
                        raise SegmentMismatch('recorded output events do not match scheduled replay')
                finally:
                    # Close suspended coroutines in their own ContextVar context.
                    # A mismatch may prevent scope-end recording; preserve the
                    # original failure and forbid reuse of the mutated engine.
                    for task in [*tasks.values(), *loops.values(), *roots, *([service] if service else [])]:
                        try:
                            task['context'].run(task['coroutine'].close)
                        except BaseException:
                            pass
        except BaseException:
            self._fail()
            raise
        finally:
            self._control_dispatch = False
            if writer_override is missing:
                engine.recorder.__dict__.pop('start_background_writer', None)
            else:
                engine.recorder.start_background_writer = writer_override
            for key, value in saved.items():
                if value is missing:
                    engine.__dict__.pop(key, None)
                else:
                    setattr(engine, key, value)
        self.last_sequence = rows[-1]['sequence']
        self._advance_origin(rows)
        if service_closed:
            engine.replay_origin['state'] = 'closed'
        return dict(inputsConsumed=cursor.index, events=events,
                    serviceLifecycleMatched=service_closed,
                    outputComparisonExclusions=['run_summary.latencyMetrics', 'run_summary.recorderHealth'],
                    outputsMatch=True if expected_events is not None else None, parityReady=False)

    async def _root(self, rows, cursor, scopes):
        row = cursor.peek()
        body, symbol = row['body'], row['symbol']
        name = body['name']
        end = (rows.scope_end(cursor.index + 1, body['id']) if getattr(rows, 'disk_backed', False) else
               next((i for i in range(cursor.index + 1, len(rows))
                     if rows[i]['kind'] == 'scope' and rows[i]['body']['phase'] == 'end'
                     and rows[i]['body']['id'] == body['id']), None))
        if end is None:
            raise SegmentMismatch('unclosed root handler')
        inputs = [] if name in ('scan', 'clock_sync') else root_inputs(rows[cursor.index:end], name)
        if name == 'market_message':
            self.transport.admit_message(symbol, inputs[0])
        context = copy_context()
        context.run(scopes.current.set, body['parentId'])
        coroutine = self._dispatch(name, symbol, inputs)
        try:
            yielded = context.run(coroutine.send, None)
        except OrderBookSequenceError:
            # Accept only a sequence failure actually raised by the shared book
            # handler, with its complete recorded scope consumed. Never swallow
            # cursor mismatches or manufacture an exception from journal text.
            if (name != 'market_message' or rows[end]['body']['outcome'] != 'raised'
                    or cursor.index != end + 1):
                raise
            return
        except StopIteration:
            return
        else:
            if isinstance(yielded, _SourcePause) and name in ('scan', 'clock_sync'):
                return dict(context=context, coroutine=coroutine, state='source_waiting',
                            source_wait=yielded.identity)
            try:
                context.run(coroutine.close)
            finally:
                raise SegmentMismatch('unsupported suspension in root handler')
