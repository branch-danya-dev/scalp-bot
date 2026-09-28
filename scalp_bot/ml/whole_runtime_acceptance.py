"""Reproducible offline whole-session semantic proof; never a market launcher.

Run: python -m scalp_bot.ml.whole_runtime_acceptance OUTPUT --model FROZEN_MODEL
The cost gate intentionally stays closed: recorded causal clock values and
RPC/stack-switch wall time cannot establish native A-F incremental latency.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path

from ..manifest_validation import fingerprint
from ..native_v5 import NativeTapeError


def save(path, value):
    Path(path).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def inventory(directory):
    return [dict(path=str(p.resolve()),bytes=p.stat().st_size,
        sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(Path(directory).rglob('*')) if p.is_file() and p.name != 'inventory.json']


def coverage(tape, result):
    """Check actual terminals, inputs and economic/label outcomes, not a flag."""
    from .dataset_promotion import digest
    names, modules, boundaries = Counter(), Counter(), Counter()
    for row in tape.events:
        if row['kind'] == 'task_open':
            modules[row['module_id']] += 1
            if row['data']['operation'] == 'runtime':
                names[row['data']['inputs']['name']] += 1
        elif row['kind'] == 'boundary':
            boundaries[row['data']['name']] += 1
    required = ('scanner','context','trade-arbiter','exchange-clock','market-AAAUSDT',
        'bybit-receive','bybit-stream','fast-eval-AAAUSDT','event-evaluation-terminal',
        'cross-venue-lifetime','cross-receive','shadow-probe-lifetime',
        'shadow-heartbeat','probe:start','probe:startup','probe:heartbeat','probe:poll',
        'probe:evaluate','probe:close','inference-worker','reply-relay')
    missing = [name for name in required if not names[name]]
    probe = result['probeCounts'] or {}
    plan_ids = {digest([p['row']['source'],p['frozenPlan']]) for p in result['prepared']}
    checks = dict(
        requiredActors=not missing,
        splitModules=all(modules[name] for name in ('core','segment','cross_venue','maker','v3','v2')),
        nativeSources=boundaries['source_ingress']>0 and boundaries['offline_wire_source']>0,
        externalReconnect=all(result['externalConnections'].get(v,0)>=2 for v in ('binance','okx')),
        ordinaryDecision=result['eventCounts'].get('decision',0)>0,
        economicPlan=any(p['economicsAllowed'] and p['frozenPlan'] for p in result['prepared']),
        fire=result['eventCounts'].get('admission_fire',0)>0,
        realFill=result['eventCounts'].get('trade_opened',0)>0,
        managedClose=any(p.get('reason') in ('stop','target','no_follow_through')
            for p in result['ordinary']['portfolio']['closed']),
        executableLabel=any(p['executable'] and p['censor_reason'] is None
            and p['cost_fill_provenance']['executor']=='PaperBroker' for p in result['labels']),
        firstPreparedPlanBinding=bool(result['labels']) and all(p['sourcePlanIdentity'] in plan_ids for p in result['labels']),
        zeroResidualExposure=not result['ordinary']['portfolio']['positions']
            and not result['ordinary']['portfolio']['pending'],
        realForecasts=probe.get('forecasts',0)>0,
        workerIsolated=result['workerExchangeImported'] is False and result['workerFailure'] is None,
        zeroProbeLoss=not any(probe.get(k,0) for k in ('queue_full','queue_expired','submit_rejected')),
        writersClean=result['researchFailure'] is None and result['writer']['writerError'] is None
            and result['writer']['droppedRows']==0,
        finalization=result['eventCounts'].get('bot_stopped',0)==1)
    return dict(status='MET' if all(checks.values()) else 'NOT_MET',checks=checks,
        missingActors=missing,actorCounts=dict(names),moduleTaskCounts=dict(modules),boundaryCounts=dict(boundaries))


def parity(expected, actual, variant):
    checks = dict(ordinary=actual['ordinaryHashes']==expected['ordinaryHashes'],
        source=actual['sourceSha256']==expected['sourceSha256'],
        externalSource=actual['externalSourceSha256']==expected['externalSourceSha256'] if variant in 'CDEF' else None,
        label=actual['labelSha256']==expected['labelSha256'] if variant in 'EF' else None,
        childOutputAndAdapter=actual['forecastSha256']==expected['forecastSha256'] if variant=='F' else None,
        probeCounts=actual['probeCounts']==expected['probeCounts'] if variant=='F' else None)
    return dict(checks=checks,passed=all(v is not False for v in checks.values()),
        ordinaryHashes=actual['ordinaryHashes'],sourceSha256=actual['sourceSha256'],
        labelSha256=actual['labelSha256'] if variant in 'EF' else None,
        forecastSha256=actual['forecastSha256'] if variant=='F' else None,
        exclusions=dict(label='v3 disabled' if variant not in 'EF' else None,
            forecast='v2 disabled' if variant!='F' else None,
            externalSource='cross_venue disabled' if variant not in 'CDEF' else None))


def cost_gap(population_hash):
    missing = ('event_loop','data_to_adapter','joined_critical_paths','source_queue_dwell',
        'callback_evaluate_residence','request_submit_queue_dwell','parent_ipc',
        'worker_scheduling','prediction','reply_ipc','relay_dwell','adapter_decision',
        'writer_codec_high_water','gc_pause_overlap','allocation_throughput_top_sites')
    return dict(populationSha256=population_hash,status='NOT_MET',
        reason='Replay clocks are frozen F causal inputs. Replay wall time includes coordinator, RPC and stack parking. Neither is a native variant cost estimator.',
        variants={v:dict(nativeCostMeasured=False,metrics={k:None for k in missing}) for v in 'ABCDEF'},
        incremental={key:None for key in ('B-A','C-B','D-C','E-D','F-E')},
        wholePopulationInstrumentationTax=dict(status='NOT_MEASURED',value=None,
            reason='No paired equivalent native scheduler population with v5 recording disabled has been established.'),
        nextRequirement='Separate real diagnostic spans from causal clocks and coordinator waits, with equivalent native source/dispatch and a recording-off control. Validate that estimator before selecting any latency fix.')


async def execute(output, model, *, repeats=2):
    # Production engine imports stay inside the parent entry point: spawning
    # __main__ must not import exchange adapters into the isolated ML process.
    from .whole_runtime_fixture import CAPTURE_ID, WholeRuntimePopulation
    from .native_runtime import NativeRuntimeActorTransport, model_hashes
    from ..native_dispatch import NativeDispatch
    from ..native_index import IndexedNativeTape
    from ..native_v5 import NativeTapeWriter, provenance
    from ..run_manifest import code_provenance, runtime_provenance
    from ..whole_runtime_replay import WholeRuntimeReplayCoordinator
    if repeats < 2:
        raise ValueError('whole-session proof requires at least two repeats')
    output = Path(output)
    output.mkdir(parents=True,exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    population = WholeRuntimePopulation(output/'capture',model_dir=model)
    source, runtime, models = code_provenance(root), runtime_provenance(), model_hashes(model)
    config = dict(settings=population.config.model_dump(exclude={'bybit_api_key','bybit_api_secret'}),model=models)
    prov = provenance(source_sha256=source['sourceSha256'],config=config,runtime=runtime)
    artifacts = {name:dict(path=str((Path(model)/name).resolve()),sha256=digest) for name,digest in models.items()}
    for relative in ('scalp_bot/ml/probe.py','scalp_bot/native_v5.py','scalp_bot/ml/whole_runtime_fixture.py'):
        artifacts[relative] = dict(path=str(root/relative),sha256=source['fileHashes'][relative])
    freeze = dict(source=source,config=config,runtime=runtime,provenance=prov,artifacts=artifacts,
        repeats=repeats,variants=list('ABCDEF'),scope='controlled offline semantic acceptance only')
    save(output/'freeze.json',freeze)

    def unchanged():
        current = code_provenance(root)
        if current != source or runtime_provenance()!=runtime or model_hashes(model)!=models:
            raise NativeTapeError('source/configured runtime/model changed during whole-session proof')
    try:
        with NativeTapeWriter(output/'tape.gz',capture_id=CAPTURE_ID+'.jsonl',provenance=prov) as writer:
            dispatch = NativeDispatch(writer)
            await dispatch.run(population.run(dispatch))
            await dispatch.join()
        unchanged()
        expected = population.result()
        save(output/'capture-result.json',expected)
        raw = inventory(output/'capture')
        save(output/'capture-inventory.json',raw)
        with IndexedNativeTape(output/'tape.gz',output/'tape.sqlite',expected_provenance=prov,
                external_inventory=raw,expected_inventory_sha256=fingerprint(raw),artifact_hashes=artifacts) as tape:
            proof = coverage(tape,expected)
            save(output/'coverage.json',proof)
            from ..pipeline_evidence import summarize
            save(output/'capture-diagnostics.json',dict(scope='actual all-on F capture only; small synthetic population, not A-F cost or W2 acceptance',
                stages=expected['recordedDiagnosticStages'],joinedItems=expected['pipelineRecords'],
                joinedSummary=summarize(expected['pipelineRecords']),nativeWriter=tape.footer['health'],
                recorder=expected['writer'],probe=expected['probeCounts']))
            if proof['status'] != 'MET':
                raise NativeTapeError('unified population coverage failed: '+str(proof['checks']))
            reports = []
            print(json.dumps(dict(capture='MET',populationSha256=tape.population_hash,tokens=len(tape.events))),flush=True)
            # Full F semantic proof precedes all projected A-E executions.
            for variant in 'FABCDE':
                for repeat in range(repeats):
                    unchanged()
                    identity = f'{variant}-{repeat+1}'
                    replay = WholeRuntimePopulation(output/f'replay-{identity}',model_dir=model,variant=variant)
                    coordinator = WholeRuntimeReplayCoordinator(tape,variant=variant,timeout_seconds=60)
                    transport = None
                    if variant == 'F':
                        transport = NativeRuntimeActorTransport(coordinator,model,expected_model_hashes=models)
                        await transport.start()
                    try:
                        report = await coordinator.run(replay.run)
                        if transport:
                            await transport.close(replay.probe.worker)
                            transport = None
                        actual = replay.result()
                        report.update(repeat=repeat+1,parity=parity(expected,actual,variant))
                        save(output/f'{identity}-result.json',actual)
                        save(output/f'{identity}-report.json',report)
                        if not report['parity']['passed'] or not report['accountingComplete']:
                            raise NativeTapeError('whole-session semantic/accounting divergence: '+identity)
                        reports.append(report)
                        unchanged()
                        print(json.dumps(dict(replay=identity,consumed=report['consumed'],excluded=report['excludedOwnedTokens'],parity=True)),flush=True)
                    finally:
                        if transport:
                            coordinator.poison(NativeTapeError('failed replay teardown'))
                            if replay.probe is not None:
                                await transport.abort_worker(replay.probe.worker)
                            await transport.close()
            summary = dict(populationSha256=tape.population_hash,provenance=prov,counts=tape.counts,
                unifiedSemanticReplay='MET',projectedAFSemanticReplay='MET',productionN30='NOT_MET',controlledW20='NOT_MET',
                nativeNaturalLabel='NOT_TESTED',reports=reports,coverage=proof,
                nextBlocker='R6 native incremental cost and whole-population instrumentation tax estimator')
            save(output/'cost-report.json',cost_gap(tape.population_hash))
            save(output/'report.json',summary)
            unchanged()
            return summary
    except BaseException as exc:
        save(output/'failure.json',dict(type=type(exc).__name__,message=str(exc)))
        raise
    finally:
        save(output/'inventory.json',inventory(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--model',required=True)
    args = parser.parse_args()
    result = asyncio.run(execute(args.output,args.model))
    print(json.dumps({k:result[k] for k in ('unifiedSemanticReplay','projectedAFSemanticReplay','productionN30','controlledW20','nextBlocker')}))


if __name__ == '__main__':
    main()
