"""Strict replay of contiguous handler segments, NOT whole-session parity.

The caller supplies the matching config and pre-state. No live loops are started.
Unsupported scheduling/interleaving is rejected rather than approximated.
"""
from contextlib import contextmanager
from copy import deepcopy

from .bybit import MarketMessage
from .domain import Candle, Candidate
from .execution import FeeSchedule
from .input_journal import InputJournal, SCHEMA, valid_body
from .input_scope import InputScopes
from .instrument import InstrumentSpec
from .manifest_validation import fingerprint
from .manifest_schema import PUBLIC_CONFIG_FIELDS
from .runtime_clock import ClockTape, ReplayRuntimeClock
from .source_await import source_await
from .source_failure import SourceFailure, RecordedSourceError


class SegmentMismatch(RuntimeError):
    pass


ROOT_INPUTS = {'bootstrap_apply': {'bootstrap'}, 'rest_context_apply': {'rest_context'},
               'start_request': {'control'}, 'toggle_strategy': {'control'}, 'stop': {'control'},
               'scan': {'scanner_result'},
               'market_message': {'market_message'}, 'clock_sync': {'clock_sample', 'clock_error'},
               'public_state': {'external'}, 'market_health': set(), 'evaluate': set(), 'arbiter': set()}


def root_inputs(rows, name):
    if name not in ROOT_INPUTS:
        raise SegmentMismatch('unsupported root handler')
    selected = [r for r in rows if r['kind'] in ROOT_INPUTS[name]]
    if name in ('start_request', 'stop', 'toggle_strategy'):
        action = {'start_request': 'set_running', 'stop': 'stop', 'toggle_strategy': 'toggle_strategy'}[name]
        selected = [r for r in selected if r['body'].get('name') == action]
    if ROOT_INPUTS[name] and len(selected) != 1:
        raise SegmentMismatch('expected exactly one handler input')
    return selected if name == 'clock_sync' else [r['body'] for r in selected]


class _DeniedRest:
    async def close(self):
        pass

    def __getattr__(self, name):
        raise SegmentMismatch("REST access forbidden in offline segment")


class _Cursor:
    market_message = InputJournal.market_message
    closed = False

    def __init__(self, rows):
        self.rows = rows
        self.index = 0
        self.scopes = None

    @property
    def sequence(self):
        return self.peek()["sequence"] - 1

    def peek(self):
        if self.index >= len(self.rows):
            raise SegmentMismatch("unexpected call after segment end")
        return self.rows[self.index]

    def close(self):
        self.append('footer', None, {'inputCount': self.sequence - 1})
        self.closed = True

    def append(self, kind, symbol, body):
        row = self.peek()
        if (kind, symbol, body) != (row['kind'], row['symbol'], row['body']):
            raise SegmentMismatch(f"input mismatch at sequence {row['sequence']}: {kind}")
        self.index += 1

    def read(self, method):
        row = self.peek()
        if row['kind'] != 'clock_read':
            raise SegmentMismatch(f"unexpected clock call at sequence {row['sequence']}")
        value = getattr(ClockTape([row['body']]), method)()
        self.append('clock_read', None, dict(method=method, value=value,
                    scopeId=self.scopes.current.get()))
        return value

    def time(self):
        return self.read('time')

    def monotonic(self):
        return self.read('monotonic')

    def perf_counter_ns(self):
        return self.read('perf_counter_ns')


def _check_segment(rows):
    allowed = {'scope', 'clock_read', 'bootstrap', 'rest_context',
               'market_message', 'callback', 'symbol_lifecycle', 'clock_sample', 'clock_error', 'external', 'scanner_result', 'source_await'}
    stack = []
    previous = None
    for index, row in enumerate(rows):
        if (row.get('schema') != SCHEMA or row.get('kind') not in allowed
                or not valid_body(row['kind'], row.get('symbol'), row.get('body'))
                or row.get('hash') != fingerprint({k: v for k, v in row.items() if k != 'hash'})):
            raise SegmentMismatch('unsupported or corrupt segment input')
        if previous is not None and (row['previousHash'] != previous['hash']
                                    or row['sequence'] != previous['sequence'] + 1):
            raise SegmentMismatch('non-contiguous segment')
        previous = row
        body = row['body']
        if row['kind'] == 'scope':
            if body['phase'] == 'begin':
                if index and (not stack or body['parentId'] != stack[-1]):
                    raise SegmentMismatch('interleaved scopes are not supported')
                stack.append(body['id'])
            else:
                if not stack or stack.pop() != body['id'] or body['outcome'] != 'returned':
                    raise SegmentMismatch('only normally returned nested scopes are supported')
                if not stack and index != len(rows) - 1:
                    raise SegmentMismatch('multiple roots in segment')
        elif not stack:
            raise SegmentMismatch('input outside root scope')
    if not rows or stack or rows[0]['kind'] != 'scope' or rows[-1]['kind'] != 'scope':
        raise SegmentMismatch('incomplete segment')


class OfflineSegmentReplay:
    """Run selected contiguous scopes against an explicitly prepared idle engine.

    This API does not certify config/pre-state provenance or full-session parity.
    Keep one instance per worker lifetime; reconnect/lifecycle needs a future driver.
    A runtime mismatch poisons the instance because engine mutations cannot be undone.
    """
    def __init__(self, engine):
        self.engine = engine
        self.handlers = {}
        self.failed = False
        self.last_sequence = 0

    def _admit_origin(self, rows):
        origin = getattr(self.engine, 'replay_origin', None)
        if origin is None:
            return
        if origin['state'] == 'failed':
            raise SegmentMismatch('discard cold engine after replay failure')
        if origin['state'] == 'closed':
            raise SegmentMismatch('replay service already closed')
        if (rows[0]['sequence'] != origin['nextSequence']
                or rows[0]['previousHash'] != origin['prefixHash']):
            raise SegmentMismatch('cold replay continuation is not contiguous')
        if fingerprint({k: getattr(self.engine.config, k) for k in PUBLIC_CONFIG_FIELDS}) != origin['configSha256']:
            raise SegmentMismatch('cold replay configuration was changed')
        if (fingerprint(self.engine.strategy_enabled) != origin['strategiesSha256']
                or fingerprint({'public': self.engine.research_policy.public(),
                                'manifest': self.engine.research_policy.manifest}) != origin['policySha256']):
            raise SegmentMismatch('cold replay strategies or policy were changed')
        if origin['state'] == 'cold' and (self.engine.sessions or self.engine.running
                or self.engine.broker.positions or self.engine.broker.pending_entries
                or self.engine.broker.balance != self.engine.config.start_balance):
            raise SegmentMismatch('cold engine pre-state was changed')

    def _advance_origin(self, rows):
        origin = getattr(self.engine, 'replay_origin', None)
        if origin is not None:
            origin.update(nextSequence=rows[-1]['sequence'] + 1, prefixHash=rows[-1]['hash'], state='replaying')
            origin['strategiesSha256'] = fingerprint(self.engine.strategy_enabled)

    def _fail(self):
        self.failed = True
        origin = getattr(self.engine, 'replay_origin', None)
        if origin is not None:
            origin['state'] = 'failed'

    async def apply(self, records, *, expected_events=None):
        rows = deepcopy(list(records))
        _check_segment(rows)
        self._admit_origin(rows)
        engine = self.engine
        if self.failed:
            raise SegmentMismatch('discard engine after previous replay failure')
        if rows[0]['sequence'] <= self.last_sequence:
            raise SegmentMismatch('segment already consumed or out of order')
        if (engine.input_journal is not None or engine._tasks or engine._event_tasks
                or engine.config.event_driven_evaluation_enabled):
            raise SegmentMismatch('requires idle engine, capture off and event scheduler disabled')
        name = rows[0]['body']['name']
        symbol = rows[0]['symbol']
        inputs = root_inputs(rows, name)
        if name != 'bootstrap_apply' and symbol is not None and symbol not in engine.sessions:
            raise SegmentMismatch('missing session pre-state')
        cursor = _Cursor(rows)
        scopes = InputScopes(cursor)
        scopes.serial = rows[0]['body']['id'] - 1
        scopes.current.set(rows[0]['body']['parentId'])
        cursor.scopes = scopes
        events = []
        try:
            with self._bindings(cursor, scopes, rows[0], events):
                await self._dispatch(name, symbol, inputs)
                if cursor.index != len(rows):
                    raise SegmentMismatch('unconsumed input observations')
                if expected_events is not None and events != expected_events:
                    mismatch = next((i for i, pair in enumerate(zip(events, expected_events))
                                     if pair[0] != pair[1]), min(len(events), len(expected_events)))
                    raise SegmentMismatch(f'recorded output events differ at index {mismatch}')
        except BaseException:
            self._fail()
            raise
        self.last_sequence = rows[-1]['sequence']
        self._advance_origin(rows)
        return {'scopeId': rows[0]['body']['id'], 'handler': name,
                'inputsConsumed': cursor.index, 'events': events,
                'outputsMatch': True if expected_events is not None else None, 'parityReady': False}

    async def _dispatch(self, name, symbol, inputs):
        engine = self.engine
        if name == 'bootstrap_apply':
            body = inputs[0]
            engine._apply_bootstrap_result(symbol, (
                InstrumentSpec(**body['instrument']) if body['instrument'] else None,
                FeeSchedule(**body['fees']) if body['fees'] else None,
                *[[Candle(**c) for c in body[k]] for k in
                  ('candles', 'context5m', 'context15m', 'context1h')]))
        elif name == 'rest_context_apply':
            engine._apply_context_result(engine.sessions[symbol], tuple(
                None if inputs[0][k] is None else [Candle(**c) for c in inputs[0][k]]
                for k in ('candles', 'context5m', 'context15m', 'context1h')))
        elif name == 'market_message':
            if symbol not in self.handlers:
                self.handlers[symbol] = engine._market_handler(symbol)[0]
            await self.handlers[symbol](MarketMessage(**inputs[0]))
        elif name == 'evaluate':
            await engine._evaluate(engine.sessions[symbol])
        elif name == 'clock_sync':
            with engine.input_scopes.enter('clock_sync'):
                await self._source_boundary(engine.input_journal, 'clock')
                row = engine.input_journal.peek()
                if row['kind'] == 'clock_sample':
                    return engine._apply_clock_sample(row['body'])
                if row['kind'] == 'clock_error':
                    return engine._apply_clock_error(row['body']['errorType'])
                raise SegmentMismatch('clock result unavailable after await')
        elif name == 'scan':
            with engine.input_scopes.enter('scan'):
                await self._source_boundary(engine.input_journal, 'scanner')
                row = engine.input_journal.peek()
                if row['kind'] != 'scanner_result':
                    raise SegmentMismatch('scanner result unavailable after await')
                await engine._apply_scanner_result([Candidate(**c) for c in row['body']['candidates']])
        elif name == 'public_state':
            engine.public_state(inputs[0]['selectedSymbol'])
        elif name == 'market_health':
            engine.market_health()
        elif name in ('start_request', 'stop', 'toggle_strategy'):
            if not getattr(self, '_control_dispatch', False):
                raise SegmentMismatch('controls require scheduled replay')
            body = inputs[0]
            if name == 'start_request':
                from .engine import TradingEngine
                TradingEngine.set_running(engine, body['value'])
            elif name == 'stop':
                engine._stop_trading(body['reason'])
            else:
                engine.toggle_strategy(body['key'], body['enabled'])
        else:
            engine._arbitrate_once()

    async def _pause_source(self, identity):
        # Segment adapter accepts adjacent wait/ready only; scheduled adapter
        # can suspend here while independent recorded handlers execute.
        pass

    async def _source_boundary(self, cursor, source, symbol=None):
        if cursor.peek()['kind'] == 'source_await':
            with source_await(self.engine, source, symbol) as identity:
                await self._pause_source(identity)
                row = cursor.peek()
                if row['kind'] == 'source_await' and row['body']['phase'] == 'failed':
                    if source not in ('scanner', 'bootstrap'):
                        raise SegmentMismatch('unsupported source failure')
                    body = row['body']
                    raise RecordedSourceError(SourceFailure(body['errorType'], body['errorMessage']))

    @contextmanager
    def _bindings(self, cursor, scopes, first, events):
        engine = self.engine
        old = (engine.clock, engine.broker.clock, engine.rest, engine.input_scopes,
               engine.recorder.clock, engine.recorder.record)
        session_clocks = {s: obj.clock for s, obj in engine.sessions.items()}
        missing = object()
        overrides = {k: engine.__dict__.get(k, missing) for k in ('_bootstrap_symbol', '_launch_symbol_worker')}
        metadata_clock = ReplayRuntimeClock(wall_seconds=first['processingWallSeconds'],
                                           mono_ns=first['processingMonoNs'])
        engine.clock = engine.broker.clock = cursor
        engine.rest = _DeniedRest()
        engine.input_journal = cursor
        engine.input_scopes = scopes
        engine.recorder.clock = metadata_clock
        def record(event, symbol, payload):
            events.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))
        engine.recorder.record = record
        async def bootstrap(symbol):
            await self._source_boundary(cursor, 'bootstrap', symbol)
            start = cursor.peek()
            if (start['kind'] != 'scope' or start['body'].get('name') != 'bootstrap_apply'
                    or start['symbol'] != symbol):
                raise SegmentMismatch('recorded bootstrap unavailable for promoted symbol')
            at = cursor.index + 1
            if at >= len(cursor.rows) or cursor.rows[at]['kind'] != 'bootstrap' or cursor.rows[at]['symbol'] != symbol:
                raise SegmentMismatch('missing promoted bootstrap result')
            await self._dispatch('bootstrap_apply', symbol, [cursor.rows[at]['body']])
        def launch_worker(symbol):
            # The transport task has no synchronous market effects. Its recorded
            # lifecycle still requires a future transport adapter; never connect.
            self.handlers.pop(symbol, None)
        engine._bootstrap_symbol = bootstrap
        engine._launch_symbol_worker = launch_worker
        for session in engine.sessions.values():
            session.clock = cursor
        try:
            yield
        finally:
            (engine.clock, engine.broker.clock, engine.rest, engine.input_scopes,
             engine.recorder.clock, engine.recorder.record) = old
            engine.input_journal = None
            for key, value in overrides.items():
                if value is missing:
                    engine.__dict__.pop(key, None)
                else:
                    setattr(engine, key, value)
            for symbol, session in engine.sessions.items():
                session.clock = session_clocks.get(symbol, engine.clock)
