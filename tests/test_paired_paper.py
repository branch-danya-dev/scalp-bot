from dataclasses import replace
import pytest
from scalp_bot.config import Settings
from scalp_bot.ml.paired_paper import PaperArm, ExperimentRanker, SharedPaperHarness
from scalp_bot.ml.prepared_dataset import PreparedForecast, intent_key
from scalp_bot.ml.contracts import SnapshotRef
from scalp_bot.ml.features import FEATURE_SCHEMA
from scalp_bot.runtime_clock import ReplayRuntimeClock
from scalp_bot.experiment_controller import HASH_FIELDS, TECHNICAL_GATES
from test_admission_pipeline import candidate


def setup(tmp_path, score=1.):
    ranker=ExperimentRanker("a"*64, lambda *a:None)
    config=Settings(_env_file=None, exchange_clock_enabled=False)
    a=PaperArm("A",config,tmp_path/"A",start_wall=1000.,start_ns=10**9)
    b=PaperArm("B",config,tmp_path/"B",start_wall=1000.,start_ns=10**9,ranker=ranker)
    for arm in (a,b):
        session,decision=candidate(arm.engine,"AAA")
        session.clock=arm.clock
        session.last_book_at=session.last_market_at=1000.
        arm.engine.running=True
        intent=arm.engine._prepare_intent(session,decision)
        if arm is b:
            source=SnapshotRef("shared","AAA",0,1,1_000_000,10**9,"capture:shared",FEATURE_SCHEMA)
            ranker.register(intent,source)
            forecast=PreparedForecast(intent_key(intent),source,"a"*64,10**9,2*10**9,.6,score,10,10,100)
            ranker.receive(forecast)
    hashes=dict.fromkeys(HASH_FIELDS,"a"*64)
    passport=dict(execution="paper", durationSeconds=1800, lossLimitUsdt=30, drawdownLimitUsdt=30,
        additionalBDrawdownUsdt=0,finalizationSeconds=90,autoRestart=False,
        gates=dict.fromkeys(TECHNICAL_GATES,"MET"),hashes=hashes)
    clock=ReplayRuntimeClock(wall_seconds=1000.,mono_ns=10**9)
    harness=SharedPaperHarness(a,b,clock=clock,emit=lambda *a:None)
    return a,b,harness,passport,hashes,forecast


def event():
    return dict(sequence=1,kind="callback",body={"name":"arbiter"},symbol=None,
        processingMonoNs=10**9,processingWallSeconds=1000.)


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [1.,-1.])
async def test_actual_admission_pass_all_and_single_veto(tmp_path,score):
    a,b,harness,p,h,f=setup(tmp_path,score)
    result=await harness.replay([event()],p,h)
    assert result["state"] == "COMPLETED"
    assert len(a.ledger()["closed"]) == 1
    assert len(b.ledger()["closed"]) == (1 if score>0 else 0)
    if score>0:
        assert a.ledger() == b.ledger()
    else:
        assert b.ledger()["balance"] == 1000
    fires=[e for e in a.engine.events if e["event"]=="admission_fire"]
    assert fires and fires[0]["payload"]["admission"]["stage"]=="FIRE"
    assert fires[0]["payload"]["admission"]["checkedAtNs"]<=fires[0]["payload"]["admission"]["fireMonoNs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",["expired","wrong_intent","wrong_source","wrong_model","worker_crash","missing"])
async def test_unavailable_B_never_falls_back_to_A(tmp_path,fault):
    a,b,harness,p,h,f=setup(tmp_path)
    if fault=="expired":b.ranker.forecasts[f.identity]=replace(f,expires_mono_ns=10**9+1)
    elif fault=="wrong_intent":b.ranker.receive(replace(f,identity="wrong"))
    elif fault=="wrong_source":b.ranker.receive(replace(f,source=replace(f.source,source_sequence=2)))
    elif fault=="wrong_model":b.ranker.receive(replace(f,model_hash="b"*64))
    elif fault=="worker_crash":b.ranker.failure="worker_crashed"
    else:b.ranker.forecasts.clear()
    row=event()
    if fault=="expired":row.update(processingMonoNs=10**9+2,processingWallSeconds=1000.000000002)
    result=await harness.replay([row],p,h)
    assert result["state"]=="INCOMPLETE"
    assert not b.ledger()["closed"]
    assert a.disabled and b.disabled and not b.broker.positions
    assert b.ranker.failure


@pytest.mark.asyncio
async def test_market_gap_and_writer_failure_preserve_failure(tmp_path):
    a,b,harness,p,h,f=setup(tmp_path)
    b.recorder.health=lambda:dict(writerError="disk failure")
    result=await harness.replay([event()],p,h)
    assert result["state"]=="INCOMPLETE"
    assert result["armSnapshots"][1]["healthy"] is False
    assert a.disabled and b.disabled


@pytest.mark.asyncio
async def test_shared_transport_gap_stops_entries_without_B_fallback(tmp_path):
    a,b,harness,p,h,f=setup(tmp_path)
    row=event()
    row.update(kind="transport",body={"phase":"fault"},symbol="AAA")
    result=await harness.replay([row,event()],p,h)
    assert result["state"] == "INCOMPLETE"
    assert a.failure == "market_gap" and a.disabled and b.disabled
    assert not a.ledger()["closed"] and not b.ledger()["closed"]


@pytest.mark.asyncio
async def test_control_fills_never_pass_native_market_gate(tmp_path):
    a,b,harness,p,h,f=setup(tmp_path)
    result=await harness.replay([event()],p,h)
    assert result["observedFills"] == [1,1]
    assert result["naturalFills"] == [0,0]
    assert result["tradingGate"] == "INCONCLUSIVE"
