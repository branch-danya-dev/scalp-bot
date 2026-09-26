"""Bounded paired paper experiment on explicit shared observations.

This is a normalized-feed runner, not an adapter for legacy session/replay logs.
No network clients, background tasks or production service are started.
"""
from copy import deepcopy
import math

from .bybit import MarketMessage
from .domain import Candidate, Candle
from .engine import TradingEngine
from .execution import FeeSchedule
from .instrument import InstrumentSpec
from .input_journal import valid_body
from .manifest_schema import PUBLIC_CONFIG_FIELDS
from .manifest_validation import fingerprint
from .offline_bootstrap import _RecordedSettings, _MemoryRecorder, _OfflineRest
from .research_policy import ResearchPolicyRuntime
from .runtime_clock import ReplayRuntimeClock


_FEED_ATOMIC_TYPES = frozenset((str, int, float, bool, type(None)))


def _copy_feed_value(value, memo=None):
    """Copy JSON-shaped feed values without dispatch/memo work for scalars.

    Retain deepcopy's alias/cycle semantics and fallback for non-JSON values;
    each portfolio still receives independent mutable containers.
    """
    kind = type(value)
    if kind in _FEED_ATOMIC_TYPES:
        return value
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if kind is dict:
        result = {}
        memo[identity] = result
        for key, item in value.items():
            result[key if type(key) is str else _copy_feed_value(key, memo)] = _copy_feed_value(item, memo)
        return result
    if kind is list:
        result = []
        memo[identity] = result
        result.extend([_copy_feed_value(item, memo) for item in value])
        return result
    return deepcopy(value, memo)


class E01Comparison:
    @staticmethod
    def _trade_ledger(engine):
        # PaperBroker.closed_trades is a UI cache limited to the last 200 trades.
        trades = [deepcopy(row['payload']) for row in engine.recorder.rows if row['event'] == 'trade_closed']
        if len(trades) != engine.broker.total_closed_trades:
            raise ValueError('incomplete comparison trade ledger')
        return trades

    def __init__(self, config, *, max_events=100_000, recorder_factory=None, experiment='E01'):
        if experiment not in ('E01', 'E06'):
            raise ValueError('unsupported paired experiment')
        self.experiment = experiment
        if config.e06_conditional_breakout_hold:
            raise ValueError('paired base config must have E06 disabled')
        if experiment == 'E06' and (config.e01_breakout_obstacle_veto or
                config.breakout_hold_without_retest_seconds != 8):
            raise ValueError('E06 requires E01 off and base hold 8s')
        if config.event_driven_evaluation_enabled or config.research_policy_mode != 'off':
            raise ValueError('E01 requires explicit evaluation callbacks and policy off')
        if type(max_events) is not int or max_events <= 0:
            raise ValueError('invalid event limit')
        self.max_events = max_events
        self.count = 0
        self.input_hash = None
        self.last_mono = None
        self.failed = False
        self.finished = False
        self.started = False
        self.engines = {}
        self.handlers = {}
        self.bootstrap = {}
        self.health = {}
        public = {key: getattr(config, key) for key in PUBLIC_CONFIG_FIELDS}
        for name, treatment in (('baseline', False), ('candidate', True)):
            settings = _RecordedSettings(**dict(public,
                e01_breakout_obstacle_veto=treatment if experiment == 'E01' else False,
                e06_conditional_breakout_hold=treatment if experiment == 'E06' else False),
                                         bybit_api_key='', bybit_api_secret='')
            clock = ReplayRuntimeClock(wall_seconds=0, mono_ns=0)
            engine = TradingEngine(settings, clock=clock, rest_client=_OfflineRest(),
                recorder=(recorder_factory(name, clock) if recorder_factory else _MemoryRecorder(clock)), research_policy=ResearchPolicyRuntime(mode='off'),
                configure_observability=False)
            engine._launch_run_timer = lambda: None  # Shared feed enforces the same deadline.
            engine._launch_symbol_worker = lambda symbol, n=name: self.handlers.pop((n, symbol), None)
            async def bootstrap(symbol, e=engine):
                if symbol not in self.bootstrap:
                    raise ValueError(f'missing shared bootstrap for {symbol}')
                body = self.bootstrap[symbol]
                e._apply_bootstrap_result(symbol, (
                    InstrumentSpec(**body['instrument']) if body['instrument'] else None,
                    FeeSchedule(**body['fees']) if body['fees'] else None,
                    *[[Candle(**c) for c in body[k]] for k in ('candles', 'context5m', 'context15m', 'context1h')]))
            engine._bootstrap_symbol = bootstrap
            self.engines[name] = engine

    @staticmethod
    def _validate(row):
        if not isinstance(row, dict) or set(row) != {'kind', 'symbol', 'body', 'wallSeconds', 'monoNs'}:
            raise ValueError('invalid shared observation envelope')
        if (type(row['wallSeconds']) not in (int, float) or not math.isfinite(row['wallSeconds'])
                or type(row['monoNs']) is not int or row['monoNs'] < 0):
            raise ValueError('invalid observation clock')
        kind, symbol, body = row['kind'], row['symbol'], row['body']
        if kind == 'scanner':
            valid = (isinstance(body, dict) and set(body) == {'candidates', 'bootstrap'}
                and isinstance(body['bootstrap'], dict)
                and valid_body('scanner_result', symbol, {'candidates': body['candidates']})
                and all(valid_body('bootstrap', s, b) for s, b in body['bootstrap'].items()))
        elif kind in ('clock_sample', 'clock_error', 'market_message', 'rest_context'):
            valid = valid_body(kind, symbol, body)
        else:
            valid = (body == {} and ((kind == 'evaluate' and isinstance(symbol, str) and bool(symbol))
                     or kind in ('arbiter', 'health', 'start', 'stop') and symbol is None))
        if not valid:
            raise ValueError('unsupported shared observation')

    async def apply(self, observation):
        if self.failed or self.finished:
            raise ValueError('comparison is failed or finished')
        row = _copy_feed_value(observation)
        try:
            self._validate(row)
            if self.count >= self.max_events or self.last_mono is not None and row['monoNs'] < self.last_mono:
                raise ValueError('event limit or monotonic clock violation')
            kind, symbol, body = row['kind'], row['symbol'], row['body']
            if kind == 'start':
                if self.started:
                    raise ValueError('one trading window per comparison')
                self.started = True
            if kind == 'scanner':
                self.bootstrap = deepcopy(body['bootstrap'])
                working = next(iter(self.engines.values())).config.working_symbols
                needed = {c['symbol'] for c in body['candidates'][:working]} - set(self.bootstrap)
                if any(needed - set(e.sessions) for e in self.engines.values()):
                    raise ValueError('scanner needs shared bootstrap for all new candidates')
            if kind in ('market_message', 'evaluate', 'rest_context') and not any(symbol in e.sessions for e in self.engines.values()):
                raise ValueError('observation has no active symbol in either portfolio')
            for name, engine in self.engines.items():
                engine.clock.set_observation(wall_seconds=row['wallSeconds'], mono_ns=row['monoNs'])
                if (engine.running and row['monoNs']/1e9 - engine._run_started_mono
                        >= engine.config.paper_run_duration_seconds):
                    engine._stop_trading('duration_elapsed')
                if kind == 'scanner':
                    await engine._apply_scanner_result([Candidate(**c) for c in deepcopy(body['candidates'])])
                elif kind == 'clock_sample': engine._apply_clock_sample(deepcopy(body))
                elif kind == 'clock_error': engine._apply_clock_error(body['errorType'])
                elif kind == 'rest_context':
                    if symbol in engine.sessions:
                        engine._apply_context_result(engine.sessions[symbol], [
                            None if body[k] is None else [Candle(**c) for c in deepcopy(body[k])]
                            for k in ('candles', 'context5m', 'context15m', 'context1h')])
                elif kind == 'market_message':
                    if symbol in engine.sessions:
                        if (name, symbol) not in self.handlers:
                            self.handlers[name, symbol] = engine._market_handler(symbol)[0]
                        handler = self.handlers[name, symbol]
                        await handler(MarketMessage(**_copy_feed_value(body)))
                elif kind == 'evaluate':
                    if symbol in engine.sessions:
                        await engine._evaluate(engine.sessions[symbol])
                elif kind == 'arbiter': engine._arbitrate_once()
                elif kind == 'health': self.health[name] = engine.market_health()
                elif kind == 'start': engine.set_running(True)
                elif kind == 'stop': engine._stop_trading('comparison_stop')
            self.count += 1
            self.last_mono = row['monoNs']
            self.input_hash = fingerprint({'previousHash': self.input_hash, 'observation': row})
        except BaseException:
            self.failed = True
            raise

    def finish(self):
        if self.failed or self.finished or not self.started:
            raise ValueError('cannot report incomplete or failed comparison')
        try:
            portfolios = {}
            for name, engine in self.engines.items():
                if engine.running:
                    engine._stop_trading('comparison_end')
                if engine.broker.positions or engine.broker.pending_entries:
                    raise ValueError('unsettled comparison portfolio')
                trades = self._trade_ledger(engine)
                portfolios[name] = dict(manifest=engine._run_manifest,
                    balance=engine.broker.balance, net=engine.broker.balance-engine.config.start_balance,
                    fees=sum(t['fees'] for t in trades), trades=trades,
                    events=deepcopy(engine.recorder.rows))
            self.finished = True
            return dict(experiment=self.experiment, status='comparison_completed', profitabilityProven=False,
                parityReady=False, eventsConsumed=self.count, sharedInputHash=self.input_hash,
                portfolios=portfolios, netDifference=portfolios['candidate']['net']-portfolios['baseline']['net'])
        except BaseException:
            self.failed = True
            raise
