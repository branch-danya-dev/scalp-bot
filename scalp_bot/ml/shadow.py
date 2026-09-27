"""Read-only ML proposal adapter and an explicit historical shadow harness."""
from dataclasses import asdict,replace
import json
from math import isfinite
from pathlib import Path
import platform
import time
import uuid

from .contracts import FeatureSnapshot,SnapshotRef,forecast_rejections
from .features import FEATURE_NAMES
from .worker import InferenceWorker
from ..domain import Action,Side,StrategyDecision


class ShadowAdapter:
    key="trend_impulse_ml"
    def __init__(self,metadata):
        self.metadata=metadata;self.seen={}

    def accept(self,forecast,current,now_ns,*,quote,instrument):
        m=self.metadata
        reasons=list(forecast_rejections(forecast,current,now_mono_ns=now_ns,max_data_age_ns=1_000_000_000,
            model_version=m["model_version"],plan_policy_version=m["policy_version"]))
        key=(current.symbol,current.selection_epoch,current.clock_domain,forecast.side)
        if forecast.source.source_sequence<=self.seen.get(key,-1):reasons.append("duplicate_proposal")
        if not isinstance(quote,(int,float)) or not isfinite(quote) or quote<=0:reasons.append("executable_quote_missing")
        if reasons:return None,tuple(reasons)
        self.seen[key]=forecast.source.source_sequence
        if forecast.p_target_first<.55:return None,("probability_abstention",)
        side=Side(forecast.side);sign=1 if side==Side.LONG else -1
        policy=m["plan_policy"]
        stop=instrument.stop_price(quote*(1-sign*policy["stop_bps"]/10000),side)
        target=instrument.target_price(quote*(1+sign*policy["target_bps"]/10000),side)
        decision=StrategyDecision(self.key,Action(side.value),["offline shadow proposal"],entry=quote,
            stop=stop,target=target,setup_id=f"ml:{current.selection_epoch}:{forecast.source.source_sequence}:{side.value}",
            details=dict(shadowOnly=True,reservesCapital=False,orderAuthority=False,
                forecast=asdict(forecast),planPolicy=policy["version"],nominalResearchUSDT=policy["nominal_usdt"]))
        return decision,()


def run_shadow(dataset_dir,model_dir,output,limit=200):
    from collections import Counter
    from .learning import load_rows
    from ..instrument import InstrumentSpec
    dataset_dir,model_dir,output=Path(dataset_dir),Path(model_dir),Path(output)
    manifest=json.loads((dataset_dir/"manifest.json").read_text())
    metadata=json.loads((model_dir/"manifest.json").read_text())
    rows=[r for r in load_rows(dataset_dir/"dataset.jsonl") if r["split"]=="test"][:limit]
    adapter=ShadowAdapter(metadata);worker=InferenceWorker(model_dir);worker.start()
    started=time.perf_counter();domain="shadow:"+uuid.uuid4().hex
    while not worker.ready and not worker.failed and time.perf_counter()-started<30:
        worker.poll();time.sleep(.005)
    if not worker.ready:
        worker.close();raise RuntimeError("worker startup failed: "+str(worker.failed))
    predictions=[];times=[];proposal_times=[];submissions=[];resources=[];reasons=Counter();current={};quotes={};originals={}
    def consume(items):
        for item in items:
            if item[0]!="forecast":reasons[item[1]]+=1;continue
            resources.append(item[3])
            f=item[1];now=time.perf_counter_ns()
            decision,why=adapter.accept(f,current[f.source.symbol],now,quote=quotes[(f.source.symbol,f.side)],
                instrument=InstrumentSpec(**manifest.get("inventory_by_capture",{}).get(f.source.capture_id,manifest["inventory"])[f.source.symbol]["instrument"]))
            reasons.update(why)
            times.append((now-f.source.available_mono_ns)/1e6)
            if decision is not None:proposal_times.append(times[-1])
            predictions.append(dict(forecast=asdict(f),original_source=originals[(f.source.symbol,f.source.source_sequence)],
                proposal=decision.public() if decision else None,reasons=why,inference_ms=item[2]/1e6))
    try:
        for row in rows:
            before=time.perf_counter_ns()
            original=SnapshotRef(**row["ref"])
            # Explicit replay mapping, retaining the historical observation fence.
            ref=replace(original,available_mono_ns=before,clock_domain=domain)
            snapshot=FeatureSnapshot(ref,FEATURE_NAMES,tuple(row["features"]))
            worker.activate(ref.symbol,ref.selection_epoch);worker.submit(snapshot,row["side"])
            current[ref.symbol]=ref;quotes[(ref.symbol,row["side"])]=row["snapshot_quote"]
            originals[(ref.symbol,ref.source_sequence)]=row["ref"]
            submissions.append((time.perf_counter_ns()-before)/1e6)
            # Harness pacing only; ordinary engine has no ML await or callback.
            for _ in range(4):
                consume(worker.poll());time.sleep(.0025)
        deadline=time.perf_counter()+2
        while (worker.inflight or worker.latest) and time.perf_counter()<deadline:
            consume(worker.poll());time.sleep(.005)
    finally:worker.close()
    def quantiles(values):
        values=sorted(values)
        return {name:values[min(len(values)-1,int((len(values)-1)*q))] if values else None
                for name,q in (("p50",.5),("p95",.95),("p99",.99))}
    report=dict(schema_version=2,model_version=metadata["model_version"],rows=len(rows),forecasts=len(predictions),
        reasons=dict(reasons),data_to_adapter_ms=quantiles(times),data_to_proposal_ms=quantiles(proposal_times),
        latency_scope="all delivered forecasts through adapter vs emitted proposals separately; dropped work has no success latency",submit_ms=quantiles(submissions),
        coalesced=worker.coalesced,dropped=worker.dropped,worker_failure=worker.failed,
        worker=worker.info,resources=resources[-1] if resources else None,
        platform=platform.platform(),shutdown_complete=not worker.process.is_alive(),
        source_mapping="original refs retained; availability translated to local replay delivery domain",
        order_authority=False,portfolio_touched=False,latency_budget_passed=bool(times and quantiles(times)["p99"]<=250))
    output.mkdir(parents=True,exist_ok=False)
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    with (output/"forecasts.jsonl").open("x") as sink:
        for row in predictions:sink.write(json.dumps(row)+"\n")
    print(json.dumps(report))
    return report
