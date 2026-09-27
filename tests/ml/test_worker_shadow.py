from dataclasses import replace
import json
import os
import time

import pytest

from scalp_bot.ml.worker import InferenceWorker
from scalp_bot.ml.contracts import FeatureSnapshot,SnapshotRef,ImpulseForecast
from scalp_bot.ml.features import FEATURE_NAMES,FEATURE_SCHEMA
from scalp_bot.ml.shadow import ShadowAdapter


def hung_worker(model,inbox,outbox,ttl):
    outbox.put(("ready",{}));inbox.get();time.sleep(60)


def crashed_worker(model,inbox,outbox,ttl):
    outbox.put(("ready",{}));inbox.get();os._exit(7)


def snapshot(seq=1,symbol="AAA",epoch=1):
    return FeatureSnapshot(SnapshotRef("test",symbol,epoch,seq,1000,time.perf_counter_ns(),"clock",FEATURE_SCHEMA),
                           FEATURE_NAMES,tuple([0.0]*len(FEATURE_NAMES)))


def poll_until(worker,condition,timeout=10):
    end=time.perf_counter()+timeout
    while not condition() and time.perf_counter()<end:
        worker.poll();time.sleep(.005)
    assert condition()


@pytest.mark.parametrize("target,reason",[(hung_worker,"prediction_timeout"),(crashed_worker,"worker_crashed")])
def test_real_spawn_hung_crashed_worker_does_not_block_submit(tmp_path,target,reason):
    worker=InferenceWorker(tmp_path,timeout_ms=50,_target=target)
    worker.start()
    try:
        poll_until(worker,lambda:worker.ready)
        worker.activate("AAA",1)
        before=time.perf_counter();assert worker.submit(snapshot(),"long")
        assert time.perf_counter()-before<.05
        poll_until(worker,lambda:worker.failed is not None)
        assert worker.failed==reason
    finally:worker.close(.1)
    assert not worker.process.is_alive()


def test_latest_mailbox_is_bounded_and_deactivation_drops_queued_work(tmp_path):
    worker=InferenceWorker(tmp_path,capacity=2)
    try:
        worker.activate("AAA",1)
        for seq in range(100):assert worker.submit(snapshot(seq),"long")
        assert len(worker.latest)==1 and worker.coalesced==99
        for s in ("BBB","CCC"):
            worker.activate(s,1);worker.submit(snapshot(symbol=s),"long")
        assert len(worker.latest)==2 and worker.dropped==1
        worker.deactivate("CCC");assert "CCC" not in worker.latest
        assert not worker.submit(snapshot(symbol="CCC"),"long")
        worker.activate("BBB",2);assert not worker.submit(snapshot(symbol="BBB",epoch=1),"long")
    finally:worker.close()


def test_parent_poll_never_touches_windows_ipc(tmp_path, monkeypatch):
    worker = InferenceWorker(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("parent event loop must not enter OS pipe polling")
    monkeypatch.setattr(worker.outbox, 'get_nowait', forbidden)
    try:
        assert worker.poll() == []
    finally:
        worker.close()


def test_shadow_admits_only_current_metadata_and_duplicate_is_rejected():
    from test_dataset_learning import instrument
    metadata=dict(model_version="m1",policy_version="p1",plan_policy=dict(version="p1",stop_bps=15,target_bps=30,nominal_usdt=100))
    adapter=ShadowAdapter(metadata);ref=snapshot().ref
    f=ImpulseForecast(ref,"m1","p1","long",30000,ref.available_mono_ns+10,ref.available_mono_ns+10**9,.7,.2,.1)
    now=ref.available_mono_ns+100
    assert adapter.accept(f,ref,now,quote=float("nan"),instrument=instrument())[1]==("executable_quote_missing",)
    decision,why=adapter.accept(f,ref,now,quote=100,instrument=instrument())
    assert decision.strategy=="trend_impulse_ml" and not why
    assert decision.details["shadowOnly"] and not decision.details["reservesCapital"]
    assert adapter.accept(f,ref,now,quote=100,instrument=instrument())[1]==("duplicate_proposal",)
    assert "selection_epoch_mismatch" in adapter.accept(f,replace(ref,selection_epoch=2),now,quote=100,instrument=instrument())[1]
    assert "clock_domain_mismatch" in adapter.accept(f,replace(ref,clock_domain="restart"),now,quote=100,instrument=instrument())[1]
    assert "model_version_mismatch" in adapter.accept(replace(f,model_version="other"),ref,now,quote=100,instrument=instrument())[1]
    assert "forecast_expired" in adapter.accept(f,ref,now+2*10**9,quote=100,instrument=instrument())[1]
