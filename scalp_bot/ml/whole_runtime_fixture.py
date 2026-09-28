"""Offline wire-source population using the unmodified production task graph.

Only REST and WebSocket IO are substituted. No strategy, admission, broker or
ledger callbacks are invoked by the source driver. Synthetic data establishes
engineering coverage, never trading edge or natural-market label acceptance.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from ..config import Settings
from ..domain import Candidate, Candle
from ..engine import TradingEngine
from ..instrument import InstrumentSpec
from ..manifest_validation import fingerprint
from ..native_controlled import VARIANTS
from ..recorder import SessionRecorder
from .prepared_dataset import PreparedDatasetCollector
from .wave2 import Wave2Observer


SYMBOL = 'AAAUSDT'
CAPTURE_ID = 'whole-runtime-offline-v1'


def _price(value):
    # A controlled wider-range market exercises the unchanged economic gates.
    return round(100+(value-100)*3, 8)


class WireSources:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.clock = dispatch.clock()
        self.topics = {}
        self.connected = asyncio.Event()
        self.raw = []
        self.book_sequence = 0

    async def active_candidates(self):
        await asyncio.sleep(0)
        return [Candidate(SYMBOL, 1e9, .02, 100., activity_rank=1)]

    async def instrument_info(self, symbol):
        return InstrumentSpec(symbol, 'Trading', .001, .001, .001, 5., 100000., 100000., 480, 10.)

    async def fee_schedule(self, symbol):
        return None

    async def clock_sample(self):
        sent = self.clock.monotonic()
        wall = self.clock.time_ns()//1_000_000
        received = self.clock.monotonic()
        return dict(server_ms=wall, sent_mono=sent, received_mono=received, received_wall_ms=wall)

    async def klines(self, symbol, interval, count):
        minute = self.clock.time_ns()//60_000_000_000*60_000
        if interval != '1':
            return []
        peaks = {12:100.,24:100.04,36:99.99,48:100.03,60:100.01,68:100.02}
        rows = []
        for i in range(80):
            if i < 74:
                base = 98.9+(i%8)*.035
                high = peaks.get(i, base+.18)
                close = min(base+.06, high-.03)
                values = base, high, base-.16, close, 120.
            else:
                close = [99.70,99.80,99.88,99.95,100.08,100.18][i-74]
                values = close-.04, close+.06, close-.14, close, 300.
            o,h,l,c,v = values
            o,h,l,c = map(_price,(o,h,l,c))
            rows.append(Candle(minute-(80-i)*60_000,o,h,l,c,v,v*c,True))
        return rows

    async def close(self):
        pass

    def connect(self, *args, **kwargs):
        sources = self
        class Socket:
            def __init__(self):
                self.queue = asyncio.Queue()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, payload, **kwargs):
                for topic in json.loads(payload)['args']:
                    sources.topics[topic] = self.queue
                if {f'orderbook.50.{SYMBOL}', f'orderbook.1000.{SYMBOL}', f'publicTrade.{SYMBOL}'} <= sources.topics.keys():
                    sources.connected.set()
            async def recv(self, **kwargs):
                return await self.queue.get()
        return Socket()

    def publish(self, topic, data, *, kind='snapshot'):
        wall = self.clock.time_ns()//1_000_000
        payload = dict(topic=topic, type=kind, ts=wall, data=data)
        self.dispatch.boundary('offline_wire_source', payload)
        self.raw.append(payload)
        self.topics[topic].put_nowait(json.dumps(payload, separators=(',', ':')).encode())

    def book(self, bid):
        self.book_sequence += 1
        for depth in (1000,50):
            self.publish(f'orderbook.{depth}.{SYMBOL}',
                dict(s=SYMBOL,b=[[str(_price(bid)),'10000']],a=[[str(_price(bid+.01)),'10000']],
                    u=self.book_sequence,seq=self.book_sequence))

    def market(self, update, bid, *, initial=False, sell=False):
        self.book(bid)
        wall = self.clock.time_ns()//1_000_000
        ticks = []
        if initial:
            ticks.extend(dict(T=wall-19_600+i*1500,p='100.00',v='1',S='Sell') for i in range(7))
        ticks.extend(dict(T=wall-4600+i*200,p=str(_price(bid-.03+i*.03/23)),
            v=str(8*max(1,update-1)**2),S='Sell' if sell else 'Buy') for i in range(24))
        self.publish(f'publicTrade.{SYMBOL}',ticks)


class WholeRuntimePopulation:
    def __init__(self, directory, *, variant='F', trade_path=True):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.variant, self.trade_path = variant, trade_path
        # Risk/economics/cadence/queue configuration remains the production default.
        self.config = Settings(_env_file=None, session_dir='whole-runtime-offline-v1', otel_enabled=False)
        self.engine = self.observer = self.sources = None

    async def run(self, dispatch):
        self.sources = sources = WireSources(dispatch)
        recorder = SessionRecorder(str(self.directory), clock=dispatch.source_clock)
        recorder.path = self.directory/(CAPTURE_ID+'.jsonl')
        observer = self.observer = Wave2Observer(self.directory/'research.gz', recorder.path.name,
            self.config, {}, native_dispatch=dispatch, modules=VARIANTS[self.variant])
        collector = PreparedDatasetCollector(self.directory/'prepared.jsonl') if 'v3' in VARIANTS[self.variant] else None
        class Engine(TradingEngine):
            def _market_stream_options(self):
                return dict(super()._market_stream_options(), connect_factory=sources.connect)
        self.engine = engine = Engine(self.config, recorder=recorder, rest_client=sources,
            capture_inputs=True, prepared_collector=collector, research_observer=observer,
            configure_observability=False, native_dispatch=dispatch)
        try:
            await engine.start()
            await sources.connected.wait()
            sources.market(1,100.16,initial=True)
            await asyncio.sleep(.15)
            engine.set_running(True)
            if self.trade_path:
                previous_bid = 100.16
                for update,bid in ((2,100.30),(3,100.32),(4,100.34),(5,100.36)):
                    for _ in range(24):
                        await asyncio.sleep(.25)
                        sources.book(previous_bid)
                    sources.market(update,bid)
                    previous_bid = bid
                await asyncio.sleep(.3)
                # Source move traverses real protective execution/label policy.
                sources.market(6,98.0,sell=True)
                await asyncio.sleep(.3)
            else:
                await asyncio.sleep(.3)
        finally:
            await engine.close()
            await observer.close(engine.clock.perf_counter_ns())
            if collector is not None:
                collector.close()
        self.raw = sources.raw
        (self.directory/'source.json').write_text(json.dumps(self.raw,indent=2)+'\n',encoding='utf-8')

    def result(self):
        from ..research_journal import read_research
        engine = self.engine
        rows = [json.loads(line) for line in engine.recorder.path.read_text(encoding='utf-8').splitlines()]
        events = [dict(event=r['event'],symbol=r['symbol'],payload=r['payload']) for r in rows if r['event'] != 'replay_input']
        research = list(read_research(self.directory/'research.gz'))
        ordinary = {name:[r for r in events if r['event'] in kinds] for name,kinds in dict(
            decision=('decision',), admission=('admission_fire','admission_rejected'),
            execution=('trade_opened','trade_closed','entry_pending','entry_cancelled')).items()}
        ordinary['portfolio'] = dict(closed=engine.broker.closed_trades,
            positions={k:asdict(v) for k,v in engine.broker.positions.items()},
            pending={k:asdict(v) for k,v in engine.broker.pending_entries.items()}, balance=engine.broker.balance)
        labels = [r['body'] for r in research if r['kind']=='prepared_label']
        prepared = [r['body'] for r in research if r['kind']=='prepared']
        return dict(sourceSha256=fingerprint(self.raw),ordinary=ordinary,
            ordinaryHashes={k:fingerprint(v) for k,v in ordinary.items()},
            labelSha256=fingerprint(labels), labels=labels,prepared=prepared,
            eventCounts=dict(__import__('collections').Counter(r['event'] for r in events)),
            researchFailure=self.observer.failure, writer=engine.recorder.health())
