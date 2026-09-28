"""Bounded public-data paper smoke. No private client or real order API."""
import os
for key in list(os.environ):
    if key.upper().startswith('SCALP_'):
        del os.environ[key]
os.environ['SCALP_DISABLE_DOTENV']='1'

import argparse
import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import asdict
import json
from pathlib import Path
import time

class ShadowProbe:
    """Frozen V2 load probe only; adapter proposals never reach admission."""
    def __init__(self, bot, model, stages):
        from scalp_bot.ml.worker import InferenceWorker
        from scalp_bot.ml.shadow import ShadowAdapter
        self.bot, self.stages = bot, stages
        self.worker = InferenceWorker(model, trace=self._timed)
        self.adapter = ShadowAdapter(json.loads((Path(model)/'manifest.json').read_text()))
        self.source, self.current, self.grid = {}, {}, {}
        self.queue = deque(maxlen=32)
        self.counts = Counter()
        self.model = model
        original = bot.input_journal.market_message
        def recorded(symbol, message):
            original(symbol, message)
            if message.receipt_mono_ns:
                self.source[symbol] = (bot.input_journal.sequence, message.receipt_mono_ns)
        bot.input_journal.market_message = recorded
        evaluate = bot._evaluate
        async def evaluated(session):
            await evaluate(session)
            if not bot.running or not self.worker.ready or not session.market_context or not session.instrument:
                return
            source = self.source.get(session.symbol)
            if source is None:
                return
            context = session.market_context
            grid = context.observed_at_ms//10_000
            if self.grid.get(session.symbol) == grid:
                return
            self.grid[session.symbol] = grid
            from scalp_bot.ml.contracts import SnapshotRef
            from scalp_bot.ml.features import FEATURE_SCHEMA, ContextCoverage, extract_context_features
            before = time.perf_counter_ns()
            ref = SnapshotRef('technical-smoke-v2-shadow', session.symbol,
                bot.router.epochs.get(session.symbol, 0), source[0], context.observed_at_ms,
                source[1], 'perf_counter', FEATURE_SCHEMA)
            covered = bot._trade_buffer_seconds(session)
            coverage = ContextCoverage(tuple(n for n in (5,15,60) if covered >= n), (),
                context.forming_candle is not None, session.deep_book_is_fresh(),
                context.structure is not None, True)
            snapshot = extract_context_features(context, ref, coverage)
            after = time.perf_counter_ns()
            stages['features'].append((after-before)/1e6)
            stages['data_to_features'].append((after-ref.available_mono_ns)/1e6)
            self.current[session.symbol] = ref
            self.worker.activate(session.symbol, ref.selection_epoch)
            for side in ('long','short'):
                if len(self.queue) == self.queue.maxlen:
                    self.counts['queue_full'] += 1
                else:
                    self.queue.append((snapshot, side, after))
            bot.recorder.record('shadow_features', session.symbol, dict(source=asdict(ref),
                features=snapshot.values, featureEndNs=after))
        bot._evaluate = evaluated

    def _timed(self, row):
        # Completed forecasts are emitted after the adapter's decision below.
        if row['terminal'] != 'forecast':
            self.bot.recorder.record('pipeline_worker', row['identity'][1], row)

    def poll(self):
        for item in self.worker.poll():
            received = time.perf_counter_ns()
            if item[0] != 'forecast':
                self.counts['worker_error:'+str(item[1])] += 1
                continue
            forecast = item[1]
            self.counts['forecasts'] += 1
            self.stages['prediction'].append(item[2]/1e6)
            session = self.bot.sessions.get(forecast.source.symbol)
            if session is None:
                reasons = ('symbol_inactive',)
            else:
                from scalp_bot.domain import Side
                _, reasons = self.adapter.accept(forecast,
                    self.current.get(session.symbol, forecast.source), received,
                    quote=session.orderbook.executable_entry(Side(forecast.side)), instrument=session.instrument)
            terminal = time.perf_counter_ns()
            if isinstance(item[-1], dict) and 'pipelineTiming' in item[-1]:
                timing=dict(item[-1]['pipelineTiming'],adapter_end_ns=terminal)
                self.bot.recorder.record('pipeline_worker', forecast.source.symbol,timing)
            self.stages['data_to_adapter'].append((terminal-forecast.source.available_mono_ns)/1e6)
            self.counts.update(reasons or ('shadow_proposal_never_executed',))
            self.bot.recorder.record('shadow_forecast', forecast.source.symbol,
                dict(forecast=asdict(forecast), reasons=reasons, receivedNs=received, adapterEndNs=terminal))
        if self.worker.ready and not self.worker.inflight and not self.worker.latest and self.queue:
            snapshot, side, feature_end = self.queue.popleft()
            if time.perf_counter_ns()-snapshot.ref.available_mono_ns < 1_000_000_000:
                self.counts['submitted' if self.worker.submit(snapshot, side, feature_ready_ns=feature_end,
                    probe_queued_ns=feature_end,probe_queue_depth=len(self.queue)) else 'submit_rejected'] += 1
            else:
                self.counts['queue_expired'] += 1
                self.bot.recorder.record('pipeline_worker',snapshot.ref.symbol,dict(
                    identity=[snapshot.ref.capture_id,snapshot.ref.symbol,snapshot.ref.selection_epoch,snapshot.ref.source_sequence,side],
                    available_ns=snapshot.ref.available_mono_ns,feature_ready_ns=feature_end,
                    terminal='probe_queue_expired',terminal_ns=time.perf_counter_ns()))


def assess_smoke(result):
    health = result.get('writerHealth', {})
    invalid = bool(result.get('error') or health.get('writerError') or health.get('inputWriterError')
        or health.get('droppedCriticalRows') or health.get('droppedRows')
        or health.get('inputAccepted') != health.get('inputWritten')
        or result.get('wave2', {}).get('failure')
        or result.get('wave2', {}).get('writer', {}).get('error')
        or result.get('transport', {}).get('backpressureEvents')
        or result.get('transport', {}).get('discardedMessages'))
    latencies = result.get('latencyMs', {})
    stages = {k: ('NOT_TESTED' if latencies.get(k, {}).get('p99') is None else
        'MET' if latencies[k]['p99'] <= limit else 'NOT_MET') for k, limit in
        (('event_loop_lateness', 20), ('data_to_adapter', 250))}
    fills = 'MET' if result.get('naturalFills', 0) > 0 else 'INCONCLUSIVE_NO_FILLS'
    return dict(integrity='INVALID' if invalid else 'REQUIRES_HASH_CHAIN_CHECK', naturalFill=fills,
        latency=stages, status='INVALID' if invalid else 'NOT_MET' if 'NOT_MET' in stages.values()
        else 'INCONCLUSIVE' if fills != 'MET' or 'NOT_TESTED' in stages.values() else 'REQUIRES_PATH_AND_CHAIN_REVIEW')


def monitor_transport(journal):
    # Transport diagnostics bypass _emit; observe the canonical journal path.
    counts = dict(backpressureEvents=0, discardedMessages=0)
    append = journal.append
    def observed(kind, symbol, body):
        append(kind, symbol, body)
        if kind == 'transport':
            counts['backpressureEvents'] += int(body.get('errorType') == 'MarketDataBackpressureError')
            counts['discardedMessages'] += body.get('discarded', 0)
    journal.append = observed
    return counts


async def wait_for_capture(bot, recorder, research, seconds, transport=None):
    deadline = time.monotonic() + seconds
    while True:
        health = recorder.health()
        supplemental = research.health() if research else {}
        failure = (recorder.inputs.error or health.get('writerError')
            or ('session rows dropped' if health.get('droppedRows') else None)
            or supplemental.get('failure') or supplemental.get('writer', {}).get('error')
            or ('transport backpressure/loss' if transport and any(transport.values()) else None))
        if failure:
            if bot.running:
                bot.set_running(False)
            raise RuntimeError('capture recording failed: ' + str(failure))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(.1, remaining))


async def main(output, seconds, shadow_model, wave2=False):
    # Spawn re-imports this script in the child. Trading dependencies belong
    # exclusively to the parent, never to the inference worker.
    from scalp_bot.demo_paper.preflight import settings, PublicRest
    from scalp_bot.capture import CaptureRecorder
    from scalp_bot.engine import TradingEngine
    from scalp_bot.paper import PaperBroker
    from scalp_bot.offline_benchmark import stats
    from scalp_bot.ml.prepared_dataset import PreparedDatasetCollector
    from scalp_bot import bybit, engine as engine_module, admission, market_runtime
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cfg = settings(output).model_copy(update=dict(paper_run_duration_seconds=seconds,
        run_label='trading-model-technical-smoke', otel_enabled=False))
    recorder = CaptureRecorder(str(output))
    from scalp_bot import pipeline_evidence
    previous_market_sink = pipeline_evidence.market_sink
    pipeline_evidence.market_sink = lambda row:recorder.record('pipeline_market',None,row)
    prepared = PreparedDatasetCollector(output/'prepared-intents.jsonl')
    bot = TradingEngine(cfg, recorder=recorder, rest_client=PublicRest(cfg),
                        capture_inputs=True, configure_observability=False, prepared_collector=prepared)
    assert type(bot.broker) is PaperBroker
    transport = monitor_transport(bot.input_journal)
    research = None
    if wave2:
        from scalp_bot.ml.wave2 import Wave2Observer
        from scalp_bot.demo_paper.preflight import source_hashes
        from scalp_bot.manifest_validation import fingerprint
        from scalp_bot.manifest_schema import PUBLIC_CONFIG_FIELDS
        manifest = dict(sourceHashes=source_hashes(), config={k: getattr(cfg, k) for k in PUBLIC_CONFIG_FIELDS},
            registryMode='shadow', makerMode='shadow', externalVenues='telemetry_only',
            labelPolicy=dict(entryLatencyMs=250, horizonMs=180000), startedWallMs=int(time.time()*1000))
        manifest['sha256'] = fingerprint(manifest)
        research = Wave2Observer(output/'wave2-research.jsonl.gz', recorder.path.name, cfg, manifest)
        bot.research_observer = research
    stages = defaultdict(list); counts=Counter(); errors=[]
    capture_market = bot.input_journal.market_message
    def captured(symbol, message):
        before = time.perf_counter_ns()
        capture_market(symbol, message)
        stages['capture_market_detach_enqueue'].append((time.perf_counter_ns()-before)/1e6)
    bot.input_journal.market_message = captured
    probe = ShadowProbe(bot, shadow_model, stages) if shadow_model else None
    for module in (bybit, engine_module, admission, market_runtime):
        original = module.observe_latency
        def observe(stage, duration, _original=original, **kwargs):
            if duration is not None:
                stages[stage].append(duration*1000)
            return _original(stage, duration, **kwargs)
        module.observe_latency=observe
    original_emit=bot._emit
    def emit(event,symbol,payload,**kwargs):
        counts[event]+=1
        if 'error' in event or 'backpressure' in str(payload).lower():
            errors.append(dict(event=event,symbol=symbol,payload=payload))
        return original_emit(event,symbol,payload,**kwargs)
    bot._emit=emit
    started=time.time(); launched=None; stop=asyncio.Event()
    async def heartbeat():
        while not stop.is_set():
            before=time.perf_counter_ns()
            await asyncio.sleep(.005)
            stages['event_loop_lateness'].append(max(0,(time.perf_counter_ns()-before-5_000_000)/1e6))
            if probe:
                probe.poll()
            if research:
                research.watch(tuple(bot.sessions))
    monitor=asyncio.create_task(heartbeat())
    failure=None
    try:
        if research:
            await research.start()
        if probe:
            probe.worker.start()
            deadline = time.monotonic()+30
            while not probe.worker.ready and not probe.worker.failed and time.monotonic()<deadline:
                await asyncio.sleep(.01)
            if not probe.worker.ready:
                raise RuntimeError(probe.worker.failed or 'shadow worker startup timeout')
            if probe.worker.info.get('exchange_imported'):
                raise RuntimeError('shadow worker imported exchange dependencies')
        await asyncio.wait_for(bot.start(),120)
        deadline=time.monotonic()+120
        while bot.start_block_reason() and time.monotonic()<deadline:
            await wait_for_capture(bot, recorder, research, 0, transport)
            await asyncio.sleep(1)
        reason=bot.start_block_reason()
        if reason:
            raise RuntimeError('smoke startup blocked: '+reason)
        await wait_for_capture(bot, recorder, research, 0, transport)
        bot.set_running(True); launched=time.time()
        print('paper smoke started',flush=True)
        await wait_for_capture(bot, recorder, research, seconds, transport)
    except Exception as exc:
        failure=f'{type(exc).__name__}: {exc}'
    finally:
        stop.set(); await monitor
        if probe:
            probe.worker.close()
        try:
            await bot.close()
        except Exception as exc:
            failure = (failure+'; ' if failure else '')+f'shutdown {type(exc).__name__}: {exc}'
        prepared.close()
        if research:
            await research.close(time.perf_counter_ns())
        health=recorder.health()
        health.update(inputWriterError=recorder.inputs.error,inputAccepted=recorder.inputs.accepted,inputWritten=recorder.inputs.written)
        health['inputDiagnostics'] = recorder.inputs.health()
        result=dict(sourceManifest=recorder.inputs.manifest,startedWall=started,launchedWall=launched,
            endedWall=time.time(),configuredSeconds=seconds,error=failure,eventCounts=dict(counts),errors=errors,
            transport=transport,
            naturalFills=sum(v['tradesOpened'] for v in bot.strategy_stats.values()),
            causalPreparedSnapshots=prepared.count,
            closedTrades=bot.broker.closed_trades,writerHealth=health,
            latencyMs={k:stats(v) for k,v in stages.items()},execution='PaperBroker; public market GET only; no credentials')
        result['shadowProbe'] = dict(scope='V2 performance probe, no ML orders or V3 training',
            counts=dict(probe.counts), workerFailure=probe.worker.failed,
            worker=probe.worker.info,
            modelSha256=__import__('hashlib').sha256((Path(shadow_model)/'model.cbm').read_bytes()).hexdigest()) if probe else None
        result['wave2'] = research.health() if research else {}
        pipeline_evidence.market_sink = previous_market_sink
        result['acceptance'] = assess_smoke(result)['status']
        result['gates'] = assess_smoke(result)
        (output/'result.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
        print(json.dumps({k:result[k] for k in ('error','naturalFills','acceptance','writerHealth')}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output');parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--shadow-model')
    parser.add_argument('--wave2', action='store_true', help='Unified segment/cross-venue/maker/V3 research capture')
    args=parser.parse_args()
    if not 1 <= args.seconds <= 900:
        parser.error('technical smoke is bounded to 1..900 seconds')
    asyncio.run(main(args.output,args.seconds,args.shadow_model,args.wave2))
