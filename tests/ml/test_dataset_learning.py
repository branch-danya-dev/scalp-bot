from dataclasses import asdict,replace
from types import SimpleNamespace
import json

import pytest

from scalp_bot.domain import Candle,OrderBook
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.ml.capture_context import CaptureContext
from scalp_bot.ml.dataset import PendingLabel,temporal_split,read_events
from scalp_bot.ml.features import extract_context_features


def instrument():
    return InstrumentSpec("AAA","Trading",.001,.001,.001,1,100000,100000,480,20)


def adapter():
    bars=[asdict(Candle(8_000_000-(70-i)*60000,100,101,99,100,10,1000)) for i in range(70)]
    return CaptureContext("AAA","unit",dict(candles=bars,instrument=asdict(instrument())),0)


def book_event(seq=1,ns=61_000_000_000,price=100,update=1):
    return dict(kind="market_message",symbol="AAA",sequence=seq,processingMonoNs=ns,
        body=dict(topic="orderbook.50.AAA",type="snapshot" if update==1 else "delta",ts=8_000_000,
                  data=dict(u=update,seq=update,b=[[str(price),"1000"]],a=[[str(price+.01),"1000"]])))


def test_causal_raw_context_feature_parity_and_frozen_prefix():
    a=adapter();a.apply(book_event())
    snapshot,context,coverage=a.snapshot(61_000_000_000)
    assert snapshot==extract_context_features(context,snapshot.ref,coverage)
    frozen=asdict(snapshot)
    # PnL and target are deliberately not adapter inputs.
    a.session.decisions["external"]={"target":10000,"pnl":999}
    assert a.snapshot(61_000_000_000)[0]==snapshot
    a.apply(book_event(2,62_000_000_000,105))
    assert asdict(snapshot)==frozen
    assert a.snapshot(62_000_000_000)[0]!=snapshot


def test_gap_requires_snapshot_and_restarts_coverage():
    a=adapter();a.apply(book_event())
    assert a.snapshot(61_000_000_000)[2].trade_windows==(5,15,60)
    a.apply(book_event(2,61_100_000_000,update=3))
    assert not a.book_state.synced
    assert not a.snapshot(61_100_000_000)[2].trade_windows
    a.apply(book_event(3,61_200_000_000))
    assert a.book_state.synced and not a.snapshot(61_200_000_000)[2].trade_windows


@pytest.mark.parametrize("side",["long","short"])
def test_executable_label_fees_depth_latency_and_censoring(side):
    from scalp_bot.ml.dataset import POLICY_PATH
    policy=json.loads(POLICY_PATH.read_text())
    a=adapter();a.apply(book_event())
    row=dict(ref=dict(available_mono_ns=61_000_000_000),side=side)
    pending=PendingLabel(row,a.epoch,91_000_000_000,61_100_000_000)
    assert pending.advance(a,61_050_000_000,policy) is None and pending.entry is None
    assert pending.advance(a,61_100_000_000,policy) is None
    sign=1 if side=="long" else -1
    price=pending.target+sign*.01
    a.session.orderbook=OrderBook([(price,1000)],[(price+.001,1000)])
    assert pending.advance(a,62_000_000_000,policy)=="target_first"
    assert row["net_usdt"]==pytest.approx(row["gross_usdt"]-row["fees_usdt"])
    assert row["fees_usdt"]==(row["entry"]+row["exit"])*row["quantity"]*policy["taker_fee_rate"]
    assert pending.advance(a,94_000_000_000,policy)=="excluded:horizon_gap"
    a.epoch+=1
    assert pending.advance(a,62_000_000_000,policy)=="excluded:book_gap"


def test_temporal_purge_is_global_across_symbols_and_entire_episode():
    rows=[dict(ref=dict(available_mono_ns=t*10**9,symbol=s),label_end_ns=(t+30)*10**9)
          for s in ("AAA","BBB") for t in (10,40,61,121,241)]
    temporal_split(rows,[60*10**9,120*10**9,180*10**9])
    assert [r["split"] for r in rows[:5]]==["purged","purged","purged","purged","test"]
    assert [r["split"] for r in rows[:5]]==[r["split"] for r in rows[5:]]


def test_training_preprocessing_only_depends_on_frozen_metadata():
    np=pytest.importorskip("numpy")
    from scalp_bot.ml.learning import transform
    prep=dict(median=[2],mean=[2],scale=[1])
    before=transform(np.asarray([[float("nan")],[3.0]]),prep)
    transform(np.asarray([[100000.0]]),prep)
    assert np.array_equal(before,transform(np.asarray([[float("nan")],[3.0]]),prep))


async def test_raw_adapter_matches_actual_engine_shared_feature_semantics(tmp_path):
    from copy import deepcopy
    from scalp_bot.config import Settings
    from scalp_bot.engine import TradingEngine
    from scalp_bot.runtime_clock import ReplayRuntimeClock
    a=adapter();a.apply(book_event())
    a.apply(dict(kind="market_message",symbol="AAA",sequence=2,processingMonoNs=61_000_000_000,
        body=dict(topic="publicTrade.AAA",ts=8_000_000,data=[
            {"T":7_999_000,"p":"100","v":"5","S":"Buy"},
            {"T":8_000_000,"p":"100.02","v":"3","S":"Sell"}])))
    snapshot,context,coverage=a.snapshot(61_000_000_000)
    engine=TradingEngine(Settings(_env_file=None,session_dir=str(tmp_path),exchange_clock_enabled=False,
        breakout_enabled=False,weak_level_rejection_enabled=False,density_enabled=False),
        clock=ReplayRuntimeClock(wall_seconds=8000,mono_ns=61_000_000_000))
    try:
        session=deepcopy(a.session);session.clock=engine.clock
        session.last_book_at=session.last_market_at=8000;session.book_synced=True
        session.last_price=session.orderbook.mid;engine.sessions["AAA"]=session
        await engine._evaluate(session)
        actual=extract_context_features(session.market_context,snapshot.ref,coverage)
        assert actual==snapshot
    finally:await engine.close()


def test_actual_cpu_training_reload_and_artifact_integrity(tmp_path):
    np=pytest.importorskip("numpy")
    pytest.importorskip("catboost")
    pytest.importorskip("sklearn")
    from scalp_bot.ml.learning import train,Predictor
    from scalp_bot.ml.features import FEATURE_NAMES,FEATURE_SCHEMA
    from scalp_bot.ml.dataset import POLICY_PATH
    from scalp_bot.ml.history.importer import sha256_file
    dataset=tmp_path/"dataset";dataset.mkdir()
    policy=json.loads(POLICY_PATH.read_text())
    rows=[]
    for split in ("train","calibration","validation","test"):
        for i in range(36):
            features=[None]*len(FEATURE_NAMES);features[0]=float(i%3);features[1]=float(i)/36
            rows.append(dict(ref=dict(symbol="SYNTHETIC"),side="long",features=features,
                label=("target_first","stop_first","timeout")[i%3],net_usdt=(.2,-.3,-.1)[i%3],split=split))
    (dataset/"dataset.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    manifest=dict(training_ready=True,feature_schema=FEATURE_SCHEMA,plan_policy=policy,
        dataset_sha256=sha256_file(dataset/"dataset.jsonl"),policy_sha256=sha256_file(POLICY_PATH))
    (dataset/"manifest.json").write_text(json.dumps(manifest))
    model=tmp_path/"model"
    report=train(dataset,model)
    assert report["reload_exact"]
    first=Predictor(model).predict_rows(rows[-1:])
    assert np.array_equal(first,Predictor(model).predict_rows(rows[-1:]))
    assert np.isfinite(first).all() and first.sum()==pytest.approx(1)
    artifact=model/"model.cbm";artifact.write_bytes(artifact.read_bytes()+b"tamper")
    with pytest.raises(ValueError,match="checksum"):
        Predictor(model)
