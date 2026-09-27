"""Sequential counterfactual study on captured inputs, never a live runner.

Processing availability orders raw observations; each variant owns a deterministic
scheduler. Recorded strategy outputs and research frames are never replay inputs.
Portfolio results are conditional on captured membership and transport availability.
"""
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import heapq
import json
from pathlib import Path
import sqlite3
import time
import zlib

from .bybit import MarketMessage, OrderBookSequenceError
from .config import Settings
from .domain import Candle, Candidate
from .engine import TradingEngine
from .execution import FeeSchedule
from .instrument import InstrumentSpec
from .offline_segment import _DeniedRest
from .parallel_scenarios import ParallelScenarioRouter, PRIORITY
from .runtime_clock import ReplayRuntimeClock
from .scenario import ScenarioRouter


class SinglePreparationRouter(ParallelScenarioRouter):
    """Original single-preparation lifecycle with current shared executor guards."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.single = ScenarioRouter(**kwargs)
        self.children = dict.fromkeys(PRIORITY, self.single)
        self.scenarios = self.single.scenarios
        self.situations = self.single.situations

    def scenario_for(self, symbol, strategy):
        scenario = self.single.scenarios.get(symbol)
        return scenario if scenario and scenario.owner == strategy else None

    def observe(self, symbol, context, candles, structure, enabled, now, **kwargs):
        scenario = self.single.observe(symbol, context, candles, structure, enabled, now, **kwargs)
        if kwargs.get('position') is not None or kwargs.get('pending') is not None:
            self.executions[symbol] = scenario
        return scenario


class StudyRecorder:
    KEEP = {'scenario_transition','decision','entry_submitted','entry_cancelled',
            'trade_opened','trade_closed','partial_take','risk_rejected','risk_reject','position_added',
            'fast_path_error','arbiter_error','bot_started','bot_stopped'}
    def __init__(self, path, clock):
        self.path, self.clock = Path(path), clock
        self.stream = self.path.open('x', encoding='utf-8')
        self.sequence = 0
        self.counts = Counter()

    def record(self, event, symbol, payload):
        self.counts[event] += 1
        if event in self.KEEP or event.startswith('scenario_') or event == 'first_reclaim':
            self.stream.write(json.dumps(dict(sequence=self.sequence, mono_ns=self.clock.perf_counter_ns(),
                wall=self.clock.time(),event=event,symbol=symbol,payload=payload), default=str)+'\n')
        if event in {'fast_path_error','arbiter_error'}:
            raise RuntimeError(f'{event}: {payload}')

    def health(self):
        return dict(background=False,queuedRows=0,writtenRows=sum(self.counts.values()),pendingRows=0,
                    queueDepth=0,queueCapacity=0,droppedRows=0,droppedBulkRows=0,droppedCriticalRows=0,writerError=None)

    def close(self):
        self.stream.close()


class StudyEngine(TradingEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scheduled = []
        self.schedule_serial = 0
        self.reclaims = set()
        self.evaluation_count = 0
        self.dataset_collector = None

    def _launch_run_timer(self):
        pass  # Captured stop availability bounds every counterfactual equally.

    def _launch_event_evaluation(self, symbol, reason, capture_id):
        session = self.sessions[symbol]
        due = max(self.clock.perf_counter_ns(),
                  int((session.last_event_eval_at+self.config.event_evaluation_min_interval_seconds)*1e9)+1)
        self.schedule_serial += 1
        heapq.heappush(self.scheduled,(due,self.schedule_serial,symbol,reason,capture_id))

    async def _event_evaluation_sleep(self, delay):
        raise RuntimeError(f'logical scheduler invoked OS sleep: {delay}')

    async def _evaluate(self, session):
        await super()._evaluate(session)
        self.evaluation_count += 1
        if self.dataset_collector is not None:
            self.dataset_collector.sample(self,session)
        state = self.strategies['weak_level_rejection']._states.get(session.symbol)
        scenario = self.router.scenario_for(session.symbol,'weak_level_rejection')
        if state and state.reclaim_at_ms:
            key = (session.symbol,state.confirmation_episode,state.zone_key,state.reclaim_at_ms)
            if key not in self.reclaims:
                self.reclaims.add(key)
                self.recorder.record('first_reclaim',session.symbol,dict(state=asdict(state),
                    scenario=scenario.public() if scenario else None,
                    best_bid=session.orderbook.best_bid,best_ask=session.orderbook.best_ask,
                    source='sequential actual WeakLevelRejectionStrategy state'))


def source_events(source):
    kinds = ('manifest','bootstrap','rest_context','scanner_result','clock_sample','clock_error',
             'market_message','transport','control','symbol_lifecycle','run_end')
    with sqlite3.connect(f'file:{Path(source).resolve().as_posix()}?mode=ro',uri=True) as db:
        placeholders = ','.join('?' for _ in kinds)
        for (payload,) in db.execute(f'SELECT payload FROM inputs WHERE kind IN ({placeholders}) ORDER BY idx',kinds):
            yield json.loads(zlib.decompress(payload))


async def advance(engine, mono, wall, next_arbiter):
    interval = int(engine.config.arbiter_interval_seconds*1e9)
    while min(engine.scheduled[0][0] if engine.scheduled else mono+1,next_arbiter) < mono:
        due = min(engine.scheduled[0][0] if engine.scheduled else mono+1,next_arbiter)
        engine.clock.set_observation(mono_ns=due,wall_seconds=wall-(mono-due)/1e9)
        if due == next_arbiter:
            if engine.running:
                engine._arbitrate_once()
            next_arbiter += interval
        else:
            _,_,symbol,reason,capture_id = heapq.heappop(engine.scheduled)
            await engine._run_event_evaluation(symbol,reason,capture_id=capture_id)
    engine.clock.set_observation(mono_ns=mono,wall_seconds=wall)
    return next_arbiter

async def study(source, output, *, variants=('A','B','C'), max_events=None, capture_id='790eebd906ac4a41baf69eccb5b69ae3'):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=False)
    events = source_events(source)
    first = next(events)
    assert first['kind']=='manifest'
    manifest = first['body']['manifest']
    (output/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    engines = {}
    handlers = {}
    arbiters = {}
    gaps = []
    started = time.perf_counter()
    for variant in variants:
        values = dict(manifest['config'])
        values.update(research_rejection_response_policy='quote_tape_v1' if variant=='C' else 'legacy')
        config = Settings(_env_file=None,**values)
        clock = ReplayRuntimeClock(mono_ns=first['processingMonoNs'],wall_seconds=first['processingWallSeconds'])
        recorder = StudyRecorder(output/f'{variant}.jsonl',clock)
        engine = StudyEngine(config,clock=clock,recorder=recorder,rest_client=_DeniedRest(),configure_observability=False)
        if variant=='A':
            engine.router = SinglePreparationRouter(preparation_seconds=config.active_symbol_idle_timeout_seconds)
        if variant=='B':
            from .ml.engine_dataset import EngineDatasetCollector
            engine.dataset_collector = EngineDatasetCollector(output/'dataset',capture_id)
        engines[variant] = engine
        handlers[variant] = {}
        arbiters[variant] = clock.perf_counter_ns()+int(config.arbiter_interval_seconds*1e9)
    rows = 0
    last = first
    try:
        for event in events:
            last = event
            rows += 1
            kind,symbol,body = event['kind'],event['symbol'],event['body']
            if kind=='run_end':
                break
            for variant,engine in engines.items():
                arbiters[variant] = await advance(engine,event['processingMonoNs'],event['processingWallSeconds'],arbiters[variant])
                engine.recorder.sequence = event['sequence']
                if kind=='bootstrap':
                    engine._apply_bootstrap_result(symbol,(InstrumentSpec(**body['instrument']) if body['instrument'] else None,FeeSchedule(**body['fees']) if body['fees'] else None,
                        *[[Candle(**c) for c in body[k]] for k in ('candles','context5m','context15m','context1h')]))
                    handlers[variant][symbol] = engine._market_handler(symbol)
                elif kind=='rest_context' and symbol in engine.sessions:
                    engine._apply_context_result(engine.sessions[symbol],tuple(None if body[k] is None else
                        [Candle(**c) for c in body[k]] for k in ('candles','context5m','context15m','context1h')))
                elif kind=='scanner_result':
                    engine.candidates = [Candidate(**c) for c in body['candidates']]
                    # Membership/REST response availability is fixed by capture, never queried.
                    for candidate in engine.candidates:
                        if candidate.symbol in engine.sessions:
                            session = engine.sessions[candidate.symbol]
                            session.last_ranked_at = engine.clock.time()
                            session.mark_price = candidate.mark_price
                            session.funding_rate = candidate.funding_rate
                            session.next_funding_time_ms = candidate.next_funding_time_ms
                elif kind=='clock_sample':
                    engine._apply_clock_sample(body)
                elif kind=='clock_error':
                    engine._apply_clock_error(body['errorType'])
                elif kind=='control':
                    if body['name']=='set_running':
                        engine.set_running(body['value'])
                    elif body['name']=='stop':
                        engine._stop_trading(body['reason'])
                    elif body['name']=='toggle_strategy':
                        engine.toggle_strategy(body['key'],body['enabled'])
                    else:
                        raise ValueError(f'unsupported control {body}')
                elif kind=='transport' and symbol in handlers[variant]:
                    _,fast,deep = handlers[variant][symbol]
                    if body['phase'] in {'fault','connecting','cancelled'}:
                        gaps.append(dict(variant=variant,sequence=event['sequence'],symbol=symbol,
                            mono_ns=event['processingMonoNs'],phase=body['phase'],topics=body['topics'],
                            position_open=symbol in engine.broker.positions))
                    engine._invalidate_transport(symbol,body,fast,deep)
                    if engine.dataset_collector and body['phase'] in {'fault','connecting','cancelled'} and any(t in {f'orderbook.50.{symbol}',f'publicTrade.{symbol}'} for t in body['topics']):
                        engine.dataset_collector.invalidate(symbol,event['processingMonoNs'])
                elif kind=='market_message' and symbol in handlers[variant]:
                    try:
                        await handlers[variant][symbol][0](MarketMessage(**body))
                        if engine.dataset_collector and body['topic'].startswith('orderbook.50.'):
                            engine.dataset_collector.quote(engine,symbol)
                    except OrderBookSequenceError as exc:
                        if engine.dataset_collector:
                            engine.dataset_collector.invalidate(symbol,event['processingMonoNs'])
                        gaps.append(dict(variant=variant,sequence=event['sequence'],symbol=symbol,
                            mono_ns=event['processingMonoNs'],phase='sequence_gap',error=str(exc),
                            position_open=symbol in engine.broker.positions))
                elif kind=='symbol_lifecycle' and body['action']!='activate':
                    raise ValueError(f'unsupported membership change {body}')
            if rows%25000==0:
                print(json.dumps(dict(rows=rows,source_sequence=event['sequence'],elapsed_seconds=round(time.perf_counter()-started,1),
                    evaluations={k:e.evaluation_count for k,e in engines.items()},trades={k:len(e.broker.closed_trades) for k,e in engines.items()})),flush=True)
            if max_events and rows>=max_events:
                break
    finally:
        for engine in engines.values():
            engine.recorder.close()
    report = dict(schema_version=1,scope='counterfactual fixed captured membership/availability; not original scheduler parity',
        rows=rows,last_sequence=last['sequence'],complete=max_events is None and last['kind']=='run_end',
        elapsed_seconds=time.perf_counter()-started,gaps=gaps,variants={})
    for variant,engine in engines.items():
        if engine.dataset_collector and engine.dataset_collector.rows:
            engine.dataset_collector.finish()
        report['variants'][variant] = dict(balance=engine.broker.balance,ledger=engine.broker.closed_trades,
            open_positions={k:asdict(v) for k,v in engine.broker.positions.items()},
            pending_entries=list(engine.broker.pending_entries),evaluations=engine.evaluation_count,
            reclaims=len(engine.reclaims),event_counts=dict(engine.recorder.counts))
    (output/'portfolio_comparison.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    print(json.dumps({k:{q:v for q,v in r.items() if q not in ('ledger','event_counts')} for k,r in report['variants'].items()},default=str),flush=True)
    return report


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('source');parser.add_argument('output')
    parser.add_argument('--capture-id',default='790eebd906ac4a41baf69eccb5b69ae3');parser.add_argument('--max-events',type=int);parser.add_argument('--variants',default='A,B,C')
    args=parser.parse_args()
    asyncio.run(study(args.source,args.output,max_events=args.max_events,variants=tuple(args.variants.split(',')),capture_id=args.capture_id))
