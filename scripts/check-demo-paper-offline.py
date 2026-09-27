"""Bounded historical integration and real saved-weight contracts; no network."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict,replace
import hashlib
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
async def replay(args,output):
    from scalp_bot.demo_paper.preflight import settings
    from scalp_bot.demo_paper.engine import PairedEngine
    from scalp_bot.demo_paper.portfolio import Arm,PairedPortfolio
    from scalp_bot.demo_paper.venues import PaperVenue
    from scalp_bot.offline_study import StudyEngine,StudyRecorder,source_events
    from scalp_bot.offline_benchmark import ingest
    from scalp_bot.offline_segment import _DeniedRest
    from scalp_bot.runtime_clock import ReplayRuntimeClock
    from scalp_bot.ml.contracts import FeatureSnapshot,SnapshotRef
    from scalp_bot.ml.features import FEATURE_NAMES
    from scalp_bot.ml.worker import InferenceWorker
    class HistoricalEngine(StudyEngine,PairedEngine):
        pass
    events=source_events(args.source);first=next(events);clock=ReplayRuntimeClock(
        mono_ns=first["processingMonoNs"],wall_seconds=first["processingWallSeconds"])
    cfg=settings(output);recorder=StudyRecorder(output/"historical-decisions.jsonl",clock)
    arms={n:Arm(n,cfg,clock,lambda *a:None) for n in ("demo","paper")}
    for arm in arms.values():arm.venue=PaperVenue(cfg,lambda *a:None,arm.fill);arm.venue.remaining=arm.remaining
    portfolio=PairedPortfolio("historical-input-contract",cfg,arms,lambda *a:None)
    engine=HistoricalEngine(cfg,portfolio,None,None,clock=clock,recorder=recorder,
        rest_client=_DeniedRest(),configure_observability=False)
    handlers={};arbiter=clock.perf_counter_ns()+int(cfg.arbiter_interval_seconds*1e9)
    counts=Counter();checksum=hashlib.sha256();last=first;processed=0
    try:
        for event in events:
            if event["processingMonoNs"]-first["processingMonoNs"]>args.seconds*1e9:break
            if processed>=args.max_events:break
            checksum.update(json.dumps(event,sort_keys=True,separators=(",",":")).encode())
            counts[event["kind"]]+=1;processed+=1;last=event
            # Captured control is retained in the digest/count; replay never
            # grants external order authority or claims to be the measured hour.
            if event["kind"]=="control":continue
            arbiter=await ingest(engine,handlers,event,arbiter)
        return dict(scope="historical raw-input integration only; no Demo matching/portfolio result",
            source=str(Path(args.source).resolve()),selected_source_sha256=checksum.hexdigest(),events=processed,
            counts=dict(counts),historical_seconds=(last["processingMonoNs"]-first["processingMonoNs"])/1e9,
            evaluations=engine.evaluation_count,symbols=list(engine.sessions),recorder_counts=dict(recorder.counts),
            positions=sum(len(a.broker.positions) for a in arms.values()),network_allowed=False,
            source_first_sequence=first["sequence"],source_last_sequence=last["sequence"])
    finally:await engine.close()

async def real_worker(args,output):
    from scalp_bot.ml.contracts import FeatureSnapshot,SnapshotRef
    from scalp_bot.ml.features import FEATURE_NAMES
    from scalp_bot.ml.worker import InferenceWorker
    rows=[]
    with (Path(args.dataset)/"dataset.jsonl").open() as file:
        for line in file:
            row=json.loads(line)
            if row["split"]=="validation":rows.append(row)
            if len(rows)>=60:break
    worker=InferenceWorker(args.model);worker.start();deadline=time.perf_counter()+30
    try:
        while not worker.ready and not worker.failed and time.perf_counter()<deadline:worker.poll();await asyncio.sleep(.005)
        if not worker.ready:raise RuntimeError("saved V2 worker startup failed")
        results=[]
        for row in rows:
            # Historical feature values unchanged; explicit local delivery clock
            # translation retained with original ref, never masquerades as live.
            original=SnapshotRef(**row["ref"]);now=time.perf_counter_ns()
            ref=replace(original,available_mono_ns=now,clock_domain="offline-worker-contract")
            worker.activate(ref.symbol,ref.selection_epoch);worker.submit(FeatureSnapshot(ref,FEATURE_NAMES,tuple(row["features"])),row["side"])
            until=time.perf_counter()+2
            while time.perf_counter()<until:
                items=worker.poll()
                if items:
                    item=items[0]
                    if item[0]!="forecast":raise RuntimeError(str(item[1]))
                    results.append(dict(original=asdict(original),forecast=asdict(item[1]),predict_ns=item[2],
                        received_ns=time.perf_counter_ns(),selected=item[1].p_target_first>=.55));break
                await asyncio.sleep(.002)
            else:raise RuntimeError("worker contract timed out")
        (output/"real-v2-forecasts.json").write_text(json.dumps(results,indent=2)+"\n")
        return dict(scope="60 pre-existing validation snapshots, unchanged weights, inference only",forecasts=len(results),
                    selected=sum(r["selected"] for r in results),worker=worker.info,order_authority=False)
    finally:worker.close()

async def main(args):
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    report={"worker":await real_worker(args,output)}
    if not args.worker_only:report["historical"]=await replay(args,output)
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n");print(json.dumps(report))

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--source",required=True);p.add_argument("--model",required=True)
    p.add_argument("--dataset",required=True);p.add_argument("--output",required=True)
    p.add_argument("--worker-only",action="store_true")
    p.add_argument("--seconds",type=int,default=180);p.add_argument("--max-events",type=int,default=100000)
    asyncio.run(main(p.parse_args()))
