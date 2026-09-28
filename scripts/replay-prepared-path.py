"""Current-source causal path on a complete saved capture, without network.

Explicit logical-scheduler counterfactual. Strict native source/tape refusal is
reported separately and never bypassed or relabelled as native parity.
"""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import gzip
import hashlib
import json
from pathlib import Path


async def run(source, output):
    from scalp_bot.capture_replay import file_sha256
    from scalp_bot.input_journal import validate_input_journal
    from scalp_bot.offline_bootstrap import restore_cold_engine, _RecordedSettings
    from scalp_bot.offline_benchmark import ingest
    from scalp_bot.offline_study import StudyEngine, StudyRecorder
    from scalp_bot.offline_segment import _DeniedRest
    from scalp_bot.runtime_clock import ReplayRuntimeClock
    from scalp_bot.ml.prepared_dataset import PreparedDatasetCollector
    from scalp_bot.ml.wave2 import Wave2Observer
    from scalp_bot.research_journal import read_research
    from scalp_bot.run_manifest import code_provenance, runtime_provenance
    import scalp_bot
    output.mkdir(parents=True,exist_ok=False)
    integrity=validate_input_journal(source)
    (output/'primary-integrity.json').write_text(json.dumps(integrity,indent=2),encoding='utf-8')
    if integrity['structuralStatus']!='checks_passed':raise ValueError('complete primary capture required')
    with gzip.open(source,'rb') as stream:prefix=[json.loads(next(stream))['payload'] for _ in range(3)]
    manifest=prefix[1]['body']['manifest']
    code=code_provenance(Path(scalp_bot.__file__).resolve().parents[1])
    native=dict(status='NOT_TESTED')
    try:restore_cold_engine(prefix)
    except Exception as exc:native=dict(status='FAIL',reason=str(exc),gate='source/runtime binding before native replay')
    cfg=_RecordedSettings(_env_file=None,**manifest['config'],bybit_api_key='',bybit_api_secret='')
    clock=ReplayRuntimeClock(wall_seconds=prefix[0]['processingWallSeconds'],mono_ns=prefix[0]['processingMonoNs'])
    class Recorder(StudyRecorder):
        KEEP=StudyRecorder.KEEP|{'admission_fire','run_summary','prepared_forecast_decision'}
    recorder=Recorder(output/'current-causal.jsonl',clock)
    collector=PreparedDatasetCollector(output/'prepared-intents.jsonl')
    engine=StudyEngine(cfg,clock=clock,recorder=recorder,rest_client=_DeniedRest(),
        configure_observability=False,prepared_collector=collector)
    observer=Wave2Observer(output/'wave2-research.jsonl.gz',recorder.path.name,cfg,
        dict(source=code,config=manifest['config'],primarySha256=file_sha256(source),
             scope='current logical replay; external venues unavailable; no native clock equivalence'))
    engine.research_observer=observer
    arbiter=clock.perf_counter_ns()+int(cfg.arbiter_interval_seconds*1e9)
    handlers={};counts=Counter();last=None;failure=None
    selected={'bootstrap','rest_context','scanner_result','clock_sample','clock_error',
        'market_message','transport','control','symbol_lifecycle','run_end'}
    try:
        with gzip.open(source,'rb') as stream:
            for line in stream:
                event=json.loads(line)['payload'];counts[event['kind']]+=1;last=event
                if event['kind'] not in selected:continue
                if event['kind']=='run_end':continue
                arbiter=await ingest(engine,handlers,event,arbiter)
                if observer.failure:raise RuntimeError(str(observer.failure))
        if engine.running or engine.broker.positions or engine.broker.pending_entries:
            raise RuntimeError('recorded stop did not close the current portfolio')
    except Exception as exc:
        failure=dict(type=type(exc).__name__,reason=str(exc))
    finally:
        await observer.close(clock.perf_counter_ns())
        collector.close();recorder.close()
    research=[]
    try:
        research=list(read_research(output/'wave2-research.jsonl.gz'))
        from scalp_bot.ml.label_replay import replay_label_inputs
        labels=replay_label_inputs(research,cfg)
    except ImportError:
        labels=dict(status='NOT_TESTED',reason='exact starting source lacks persisted label-input replay')
    except ValueError as exc:
        labels=dict(status='FAIL',reason=str(exc),scope='supplemental integrity before label replay')
    complete=[r['body'] for r in research if r['kind']=='prepared_label' and r['body']['trainingReady']]
    report=dict(source=code,sourceUnchanged=code_provenance(Path(scalp_bot.__file__).resolve().parents[1])['sourceSha256']==code['sourceSha256'],
        runtime=runtime_provenance(),primarySha256=file_sha256(source),configSha256=manifest['configSha256'],
        counts=dict(counts),completePopulation=last is not None and last['kind']=='footer',
        nativeReplay=native,labelReplay=labels,scope='full saved input population inspected; source events executed by current logical scheduler',
        naturalMarketGate='NOT_TESTED',nativeExecutableLabelGate='INCONCLUSIVE',
        prepared=collector.count,completeLabels=len(complete),eventCounts=dict(recorder.counts),
        ledger=engine.broker.closed_trades,balance=engine.broker.balance,
        openPositions=list(engine.broker.positions),pending=list(engine.broker.pending_entries),
        failure=failure,observerHealth=observer.health(),trainingReady=False,promotionAuthorized=False)
    (output/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('prepared','completeLabels','balance','failure','nativeReplay','labelReplay')},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();asyncio.run(run(args.source,args.output))
