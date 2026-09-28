from copy import deepcopy
import pytest
from scalp_bot.ml.dataset_promotion import assess_population, digest, POLICY_HASH
from scalp_bot.ml.prepared_learning import verify_dataset_evidence, fit, matrix
from scalp_bot.ml.prepared_population import join_capture


def population():
    captures={f"c{i}":dict(startWallMs=1_790_553_600_000+i*43_200_000,
        endWallMs=1_790_553_600_000+i*43_200_000+600_000,
        primaryIntegrity="MET",labelReplay="MET",populationComplete=True,
        sourceHash="a"*64,configHash="b"*64,runtimeHash="c"*64) for i in range(6)}
    rows=[]
    for i in range(2016):
        capture=f"c{i//336}";symbol=f"S{i%6}";start=captures[capture]["startWallMs"]+(i%336)*1000
        source=dict(capture_id=capture,symbol=symbol,clock_domain="capture:"+capture)
        plan=dict(quantity=1,entry=100)
        rows.append(dict(identity=str(i),capture_id=capture,symbol=symbol,source=source,
            available_wall_ms=start,label_end_wall_ms=start+500,wall_time_provenance="local_utc_at_preparation",
            economicsAllowed=True,trainingReady=True,censor_reason=None,realized_net_r=(-1 if i%2 else 1),
            target_before_stop=bool(i%2),strategy="level_breakout" if i%2 else "weak_level_rejection",
            segment={k:str(i%2) for k in ("localRegime","htfAlignment","flowAlignment")},
            planHash=digest(plan),sourcePlanIdentity=digest([source,plan]),
            cost_fill_provenance=dict(executor="PaperBroker",events=[{}],trade={"reason":"stop"},
                frozenPlan=plan,entryDepth={"fresh":True},instrument={"tick_size":.01})))
    return rows,captures


def test_population_gate_is_outcome_blind_and_requires_each_coverage_axis():
    rows,captures=population()
    assert assess_population(rows,captures)["status"]=="MET"
    for row in rows:row["realized_net_r"]=-1000
    assert assess_population(rows,captures)["status"]=="MET"  # economics promotion is separate
    for row in rows:row["segment"]["flowAlignment"]="unknown"
    result=assess_population(rows,captures)
    assert result["status"]=="NOT_MET" and "flowAlignment" in result["reasons"]


def test_overlapping_capture_domains_cannot_fake_independent_periods():
    rows,captures=population()
    for r in rows:
        r["available_wall_ms"]=1_790_553_600_000+1000;r["label_end_wall_ms"]=1_790_553_600_000+2000
    for proof in captures.values():
        proof.update(startWallMs=1_790_553_600_000,endWallMs=1_790_553_600_000+600_000)
    result=assess_population(rows,captures)
    assert result["counts"]["independentPeriods"]==1 and result["status"]=="NOT_MET"


def test_direct_fit_is_fail_closed_and_policy_hash_is_binding():
    with pytest.raises(ValueError,match="dataset evidence"):fit([],{},"logistic")
    rows,captures=population()
    evidence=dict(datasetHash=digest(rows),trainingReady=True,captures=captures,populationPolicyHash=POLICY_HASH)
    verify_dataset_evidence(rows,evidence)
    evidence["populationPolicyHash"]="a"*64
    with pytest.raises(ValueError,match="promotion gate"):verify_dataset_evidence(rows,evidence)


def test_bybit_ablation_does_not_relabel_or_impute_missing_venue():
    pytest.importorskip("numpy")
    row=dict(features=[1.,2.],side="long",crossVenue={})
    assert matrix([row],"bybit_only").shape==(1,3)
    assert matrix([row],"bybit_cross_venue").shape[1]>3
    assert row["crossVenue"]=={}


def test_economic_rejections_remain_in_observation_population():
    raw=dict(row=dict(identity="i",source=dict(capture_id="c",symbol="A",clock_domain="capture:c"),
        intent=dict(strategy="level_breakout",side="long"),features=[1.],available_wall_ms=1000,
        wall_time_provenance="local_utc_at_preparation"),frozenPlan=None,segment={},crossVenue={},
        crossVenueAlignment="unavailable",economicsAllowed=False,economicReason="cost")
    rows=join_capture([raw],[],capture_id="c")
    assert len(rows)==1 and rows[0]["realized_net_r"] is None and not rows[0]["trainingReady"]
    with pytest.raises(ValueError,match="duplicate"):join_capture([raw,raw],[],capture_id="c")
