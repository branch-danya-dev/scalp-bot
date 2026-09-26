"""Read-only live adapter for a bounded E01 technical smoke run.

All state mutations are serialized into a sealed shared feed. Network requests
run outside that critical section. Reconnects/errors invalidate this smoke run;
there is deliberately no silent continuation across an unrecorded market gap.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import hashlib
from pathlib import Path
import shutil
import time
import zipfile

from .bybit import BybitRestClient, stream_symbol
from .e01_comparison import E01Comparison
from .input_journal import MARKET_FIELDS
from .manifest_schema import PUBLIC_CONFIG_FIELDS
from .manifest_validation import fingerprint
from .offline_bootstrap import _RecordedSettings
from .run_manifest import code_provenance, runtime_provenance


def write_json(stream, value):
    return stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')


def observe_duration(stats, name, started):
    elapsed_ms = max(0, time.perf_counter_ns() - started) / 1e6
    entry = stats.setdefault(name, dict(count=0, totalMs=0.0, maxMs=0.0))
    entry['count'] += 1
    entry['totalMs'] += elapsed_ms
    entry['maxMs'] = max(entry['maxMs'], elapsed_ms)


def smoke_settings(overrides, *, max_duration_seconds=3600):
    if not isinstance(overrides, dict) or set(overrides) - PUBLIC_CONFIG_FIELDS:
        raise ValueError('profile must contain only public settings')
    config = _RecordedSettings(**overrides, bybit_api_key='', bybit_api_secret='')
    if not 60 <= config.paper_run_duration_seconds <= max_duration_seconds:
        raise ValueError(f'technical smoke duration must be 60..{max_duration_seconds} seconds')
    if (not config.exchange_clock_enabled or config.fee_rate_mode != 'configured'
            or config.event_driven_evaluation_enabled or config.research_policy_mode != 'off'):
        raise ValueError('smoke requires exchange clock, configured fees, policy off and event evaluation off')
    if min(config.clock_sync_interval_seconds, config.scanner_interval_seconds,
           config.arbiter_interval_seconds) <= 0:
        raise ValueError('poll intervals must be positive')
    return config


class DiskRecorder:
    """Full events on disk; only closed trades retained for the final ledger."""
    def __init__(self, path, clock):
        self.path = str(path)
        self.clock = clock
        self.stream = Path(path).open('x', encoding='utf-8')
        self.rows = []
        self.error = None
        self.timings = {}
        self.characters = {}

    def record(self, event, symbol, payload):
        started = time.perf_counter_ns()
        row = dict(event=event, symbol=symbol, payload=payload)
        size = write_json(self.stream, dict(row, wallSeconds=self.clock.time(), monoNs=self.clock.perf_counter_ns()))
        self.characters[event] = self.characters.get(event, 0) + size
        if event == 'trade_closed':
            self.rows.append(deepcopy(row))
        if event.endswith('_error'):
            self.error = event
        observe_duration(self.timings, event, started)

    def health(self):
        return dict(mode='e01_disk', pending=0, dropped=0, writerError=None)

    def close(self):
        self.stream.close()


class LiveSmoke:
    def __init__(self, config, directory, *, rest=None, streamer=stream_symbol, max_events=5_000_000,
                 experiment='E01', purpose='technical_smoke', min_free_bytes=0):
        if experiment not in ('E01', 'E06'):
            raise ValueError('unsupported paired experiment')
        self.experiment, self.purpose = experiment, purpose
        self.min_free_bytes = min_free_bytes
        self._last_disk_check = None
        self.config = config
        self.directory = Path(directory)
        self.rest = rest
        self.streamer = streamer
        self.max_events = max_events
        self.lock = asyncio.Lock()
        self.workers = {}
        self.tasks = []
        self.recorders = {}
        self.failure = None
        self.closing = False
        self.transport_stream = None
        self.feed = None
        self.pair = None
        self.peak = {}
        self.max_drawdown = {}
        self.equity_samples = 0
        self.processing_timings = {}
        self.feed_write_timings = {}
        self.lock_wait_timings = {}
        self.backpressure_errors = []

    def active_symbols(self):
        return set().union(*(set(e.sessions) for e in self.pair.engines.values()))

    def recorder(self, name, clock):
        recorder = DiskRecorder(self.directory / f'{name}-events.jsonl', clock)
        self.recorders[name] = recorder
        return recorder

    async def emit(self, kind, symbol=None, body=None):
        waiting = time.perf_counter_ns()
        async with self.lock:
            if self.closing:
                return  # Late source callbacks must not append after the sealed stop.
            observe_duration(self.lock_wait_timings, kind, waiting)
            if self.failure:
                raise RuntimeError(self.failure)
            if kind in ('market_message', 'rest_context') and symbol not in self.active_symbols():
                return  # A completed request for a deactivated symbol is irrelevant to both.
            row = dict(kind=kind, symbol=symbol, body=body if body is not None else {},
                       wallSeconds=time.time(), monoNs=time.perf_counter_ns())
            previous = self.pair.input_hash
            writing = time.perf_counter_ns()
            digest = fingerprint(dict(previousHash=previous, observation=row))
            write_json(self.feed, dict(sequence=self.pair.count + 1, previousHash=previous,
                                      observation=row, hash=digest))
            observe_duration(self.feed_write_timings, kind, writing)
            applying = time.perf_counter_ns()
            try:
                await self.pair.apply(row)
            finally:
                observe_duration(self.processing_timings, kind, applying)
            for recorder in self.recorders.values():
                if recorder.error:
                    raise RuntimeError(f'engine error: {recorder.error}')
            if kind in ('health', 'stop'):
                self.sample_equity(row)
            return row

    def sample_equity(self, row):
        values = {}
        for name, engine in self.pair.engines.items():
            equity = engine.broker.balance + sum(p.unrealized_pnl for p in engine.broker.positions.values())
            peak = max(self.peak.get(name, engine.config.start_balance), equity)
            self.peak[name] = peak
            self.max_drawdown[name] = max(self.max_drawdown.get(name, 0), peak - equity)
            values[name] = dict(balance=engine.broker.balance, equity=equity,
                                openPositions=len(engine.broker.positions), marketHealth=self.pair.health.get(name))
        write_json(self.equity_stream, dict(wallSeconds=row['wallSeconds'], monoNs=row['monoNs'], portfolios=values))
        self.equity_samples += 1

    def transport(self, symbol, event):
        write_json(self.transport_stream, dict(symbol=symbol, wallSeconds=time.time(), **event))
        if not self.closing and (event['phase'] in ('fault', 'drained', 'cancelled')
                                 or event['attempt'] > 1):
            # Cancellation of a deliberately retired worker is accounted for separately.
            worker = self.workers.get(symbol)
            if worker is not None and not worker[1].is_set():
                self.failure = self.failure or f'market transport interrupted: {symbol}/{event["phase"]}/{event.get("errorType")}'

    def backpressure(self, symbol, event):
        detail = dict(symbol=symbol, wallSeconds=time.time(), **event)
        self.backpressure_errors.append(detail)
        self.backpressure_errors = self.backpressure_errors[-10:]
        write_json(self.transport_stream, dict(phase='backpressure_detail', **detail))

    def performance(self):
        return dict(processing=self.processing_timings, feedHashAndWrite=self.feed_write_timings,
                    lockWait=self.lock_wait_timings, backpressure=self.backpressure_errors,
                    recorders={name: dict(timings=r.timings, writtenCharacters=r.characters)
                               for name, r in self.recorders.items()})

    async def sync_workers(self):
        active = self.active_symbols()
        for symbol in set(self.workers) - active:
            task, stop = self.workers.pop(symbol)
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for symbol in sorted(active - set(self.workers)):
            stop = asyncio.Event()
            async def callback(message, s=symbol):
                await self.emit('market_message', s, {k: getattr(message, k) for k in MARKET_FIELDS})
            task = asyncio.create_task(self.streamer(self.config.bybit_public_ws_url, symbol, callback, stop,
                fast_orderbook_depth=self.config.fast_orderbook_depth,
                deep_orderbook_depth=self.config.deep_orderbook_depth,
                market_queue_size=self.config.market_queue_size,
                market_queue_put_timeout_seconds=self.config.market_queue_put_timeout_seconds,
                market_queue_max_lag_seconds=self.config.market_queue_max_lag_seconds,
                on_backpressure=lambda event, s=symbol: self.backpressure(s, event),
                on_transport=lambda event, s=symbol: self.transport(s, event)))
            self.workers[symbol] = task, stop

    async def scan(self):
        candidates = await self.rest.active_candidates()
        bootstrap = {}
        for candidate in candidates[:self.config.working_symbols]:
            symbol = candidate.symbol
            if all(symbol in e.sessions for e in self.pair.engines.values()):
                continue
            instrument = await self.rest.instrument_info(symbol)
            fees = await self.rest.fee_schedule(symbol)
            body = dict(instrument=asdict(instrument) if instrument is not None else None,
                        fees=asdict(fees) if fees is not None else None)
            for key, interval, limit in self.candle_requests():
                body[key] = [asdict(c) for c in await self.rest.klines(symbol, interval, limit)]
            bootstrap[symbol] = body
        await self.emit('scanner', body=dict(candidates=[asdict(c) for c in candidates], bootstrap=bootstrap))
        await self.sync_workers()

    def candle_requests(self):
        return [('candles', '1', self.config.bootstrap_1m_candles),
                ('context5m', '5', self.config.bootstrap_5m_candles),
                ('context15m', '15', self.config.bootstrap_15m_candles),
                ('context1h', '60', self.config.bootstrap_1h_candles)]

    async def context(self):
        for symbol in sorted(self.active_symbols()):
            body = {}
            # Refresh 1m only when either portfolio needs it, sharing the same result.
            need_1m = any(e._context_needs_1m(e.sessions[symbol]) for e in self.pair.engines.values()
                          if symbol in e.sessions)
            for key, interval, limit in self.candle_requests():
                body[key] = (None if key == 'candles' and not need_1m else
                             [asdict(c) for c in await self.rest.klines(symbol, interval, min(limit, 240) if key == 'candles' else limit)])
            await self.emit('rest_context', symbol, body)

    async def sync_clock(self):
        await self.emit('clock_sample', body=await self.rest.clock_sample())

    async def periodic(self, interval, operation):
        while True:
            await asyncio.sleep(interval)
            await operation()

    def check_tasks(self):
        if self.min_free_bytes and (self._last_disk_check is None or time.perf_counter()-self._last_disk_check >= 60):
            self._last_disk_check = time.perf_counter()
            if shutil.disk_usage(self.directory).free < self.min_free_bytes:
                raise RuntimeError('capture disk reserve exhausted')
        if self.failure:
            raise RuntimeError(self.failure)
        for task in [*self.tasks, *(worker[0] for worker in self.workers.values())]:
            if task.done():
                if not task.cancelled() and task.exception():
                    raise task.exception()
                raise RuntimeError('live source task stopped unexpectedly')

    async def run(self):
        # Refuse an existing directory, including an empty one: never overwrite evidence.
        self.directory.mkdir(parents=True, exist_ok=False)
        result = None
        try:
            self.feed = (self.directory / 'feed.jsonl').open('x', encoding='utf-8')
            self.transport_stream = (self.directory / 'transport.jsonl').open('x', encoding='utf-8')
            self.equity_stream = (self.directory / 'equity.jsonl').open('x', encoding='utf-8')
            self.pair = E01Comparison(self.config, max_events=self.max_events, recorder_factory=self.recorder,
                                     experiment=self.experiment)
            header = dict(schema='e01-feed-v2', config={k: getattr(self.config, k) for k in PUBLIC_CONFIG_FIELDS},
                code=code_provenance(Path(__file__).resolve().parents[1]), runtime=runtime_provenance())
            if self.experiment == 'E06':
                header.update(schema='paired-feed-v3', experiment='E06')
                root = Path(__file__).resolve().parents[1]
                with zipfile.ZipFile(self.directory/'source-at-capture.zip','x',zipfile.ZIP_DEFLATED) as archive:
                    for name, expected in header['code']['fileHashes'].items():
                        content = (root/name).read_bytes()
                        if hashlib.sha256(content).hexdigest() != expected:
                            raise ValueError('source changed while archiving capture')
                        archive.writestr(name,content)
            write_json(self.feed, header)
            self.pair.input_hash = fingerprint(header)  # Bind every observation to config/source/runtime.
            if self.rest is None:
                self.rest = BybitRestClient(self.config)
            await self.sync_clock()
            self.tasks.append(asyncio.create_task(self.periodic(self.config.clock_sync_interval_seconds, self.sync_clock)))
            await self.scan()
            self.tasks.extend([
                asyncio.create_task(self.periodic(self.config.scanner_interval_seconds, self.scan)),
                asyncio.create_task(self.periodic(60, self.context)),
            ])
            warmup_deadline = time.perf_counter() + max(60, self.config.market_preflight_timeout_seconds)
            while True:
                self.check_tasks()
                await self.emit('health')  # Health reads mutate diagnostic clock state: journal them too.
                if all(state['ready'] for state in self.pair.health.values()):
                    break
                if time.perf_counter() >= warmup_deadline:
                    reasons = {n: e.start_block_reason() for n, e in self.pair.engines.items()}
                    raise RuntimeError(f'market preflight timed out: {reasons}')
                await asyncio.sleep(0.1)
            start = await self.emit('start')
            print(f'{self.experiment} paper capture started; shared feed and two portfolios are being recorded.', flush=True)
            deadline = start['monoNs'] / 1e9 + self.config.paper_run_duration_seconds
            while time.perf_counter() < deadline:
                self.check_tasks()
                await self.emit('arbiter')
                await self.emit('health')
                if time.perf_counter() < deadline and not all(e.running for e in self.pair.engines.values()):
                    raise RuntimeError('portfolio stopped before the shared deadline')
                await asyncio.sleep(min(self.config.arbiter_interval_seconds, max(0, deadline - time.perf_counter())))
            self.check_tasks()
            await self.emit('stop')
            self.closing = True
            if self.experiment == 'E06' and code_provenance(Path(__file__).resolve().parents[1])['sourceSha256'] != header['code']['sourceSha256']:
                raise ValueError('source changed during E06 capture')
            result = self.pair.finish()
            for name, portfolio in result['portfolios'].items():
                portfolio.update(eventsFile=f'{name}-events.jsonl', embeddedEvents='closed_trades_only',
                                 sampledMaxDrawdownUsd=self.max_drawdown.get(name, 0))
            result.update(purpose=self.purpose, longRunReady=False, equitySamples=self.equity_samples,
                          equityValuation='broker cached unrealized PnL; sampled, not tick-exact drawdown')
            for recorder in self.recorders.values():
                recorder.stream.flush()
            self.transport_stream.flush()
            self.equity_stream.flush()
            # A footer is written only after all inputs and settlement succeeded.
            write_json(self.feed, dict(footer=True, eventsConsumed=self.pair.count, sharedInputHash=self.pair.input_hash))
            self.feed.flush()
            with (self.directory / 'result.json').open('x', encoding='utf-8') as stream:
                write_json(stream, result)
            return result
        except BaseException as exc:
            with (self.directory / 'failure.json').open('x', encoding='utf-8') as stream:
                write_json(stream, dict(status='invalid', errorType=type(exc).__name__, error=str(exc),
                    eventsConsumed=self.pair.count if self.pair else 0, profitabilityProven=False))
            raise
        finally:
            self.closing = True
            tasks = [*self.tasks, *(worker[0] for worker in self.workers.values())]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                with (self.directory / 'performance.json').open('x', encoding='utf-8') as stream:
                    write_json(stream, self.performance())
            finally:
                try:
                    if self.rest is not None:
                        await self.rest.close()
                finally:
                    for recorder in self.recorders.values():
                        recorder.close()
                    for stream in (self.feed, self.transport_stream, getattr(self, 'equity_stream', None)):
                        if stream is not None:
                            stream.close()
