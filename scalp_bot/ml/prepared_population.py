"""Join complete immutable first-prepared populations across independent runs."""
from copy import deepcopy
import json
from pathlib import Path

from .dataset_promotion import POLICY_HASH, assess_population, digest


def join_capture(prepared, labels, *, capture_id):
    indexed={}
    for label in labels:
        key=label["identity"]
        if key in indexed:raise ValueError("duplicate label identity")
        indexed[key]=label
    rows=[];seen=set()
    for observation in prepared:
        frozen=observation["row"]
        identity=frozen["identity"]
        source=frozen["source"]
        if identity in seen or source["capture_id"]!=capture_id:
            raise ValueError("duplicate prepared or wrong capture domain")
        seen.add(identity)
        label=indexed.pop(identity,None)
        if label and (label["source"]!=source or label.get("capture_id",capture_id)!=capture_id):
            raise ValueError("label source differs from first-prepared source")
        plan=observation.get("frozenPlan")
        if label and label.get("cost_fill_provenance",{}).get("frozenPlan")!=plan:
            raise ValueError("label frozen plan differs from preparation")
        row=deepcopy(label or {})
        row.update(identity=identity,source=deepcopy(source),capture_id=capture_id,
            symbol=source["symbol"],strategy=frozen["intent"]["strategy"],side=frozen["intent"]["side"],
            intent=deepcopy(frozen["intent"]),features=deepcopy(frozen["features"]),
            available_wall_ms=frozen.get("available_wall_ms"),wall_time_provenance=frozen.get("wall_time_provenance"),
            segment=deepcopy(observation["segment"]),crossVenue=deepcopy(observation["crossVenue"]),
            crossVenueAlignment=observation["crossVenueAlignment"],
            economicsAllowed=observation["economicsAllowed"],economicReason=observation["economicReason"],
            frozenPlan=deepcopy(plan),planHash=digest(plan) if plan else None,
            sourcePlanIdentity=digest([source,plan]) if plan else None,
            preparedHash=digest(observation))
        if not observation["economicsAllowed"]:
            if label and label.get("trainingReady"):
                raise ValueError("economic rejection has executable outcome")
            row.update(trainingReady=False,realized_net_r=None,netPnl=None,
                censor_reason="economics_rejected:"+observation["economicReason"])
        elif label is None:
            row.update(trainingReady=False,realized_net_r=None,netPnl=None,censor_reason="missing_executable_label")
        rows.append(row)
    if indexed:raise ValueError("orphan labels outside prepared population")
    return rows


def assemble(captures, output):
    """Each input contains prepared/labels plus its primary and replay receipts.

    Missing evidence is preserved as NOT_MET. This function does not certify
    caller-supplied evidence or replace independently verified raw hash chains.
    """
    rows=[];proofs={}
    for capture in captures:
        identity=capture["capture_id"]
        if identity in proofs:raise ValueError("capture imported twice")
        proofs[identity]=deepcopy(capture["proof"])
        joined=join_capture(capture["prepared"],capture["labels"],capture_id=identity)
        # Count includes every rejection/censor, never only returned complete labels.
        if proofs[identity].get("preparedCount")!=len(joined):
            proofs[identity]["populationComplete"]=False
        rows.extend(joined)
    rows.sort(key=lambda r:(r.get("available_wall_ms") is None,r.get("available_wall_ms") or 0,r["capture_id"],r["identity"]))
    gate=assess_population(rows,proofs)
    evidence=dict(schema="prepared-multicapture-population-v1",datasetHash=digest(rows),
        populationPolicyHash=POLICY_HASH,trainingReady=gate["status"]=="MET",captures=proofs,gate=gate,
        timeDomain="global_local_utc; monotonic remains capture-local",promotionAuthorized=False)
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    with (output/"population.jsonl").open("x",encoding="utf-8") as stream:
        for row in rows:stream.write(json.dumps(row,sort_keys=True,allow_nan=False)+"\n")
    (output/"manifest.json").write_text(json.dumps(evidence,indent=2,allow_nan=False),encoding="utf-8")
    return evidence
