import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time
import pytest
from .test_contracts import make_pair, plan, execution, command, MockRest
from scalp_bot.demo_paper.contracts import Intent, SafetyError
from scalp_bot.demo_paper.engine import PairedEngine
from scalp_bot.demo_paper.ml_adapter import ResearchMLAdapter
from scalp_bot.demo_paper.preflight import local_preflight, settings, ROOT, connected_preflight
from scalp_bot.demo_paper.runtime import install_stop_signals, start_worker_without_secrets
from scalp_bot.demo_paper.transport import Credentials
from scalp_bot.demo_paper.venues import DemoVenue
from scalp_bot.ml.contracts import SnapshotRef, ImpulseForecast
from scalp_bot.ml.features import FEATURE_SCHEMA
from scalp_bot.domain import Side, OrderBook
from scalp_bot.engine import ActiveSymbolSession
from scalp_bot.offline_segment import _DeniedRest


def metadata():
    return json.loads((ROOT/"docs/pr58-readiness/model-manifest-v2.json").read_text())


@pytest.mark.parametrize("side",["long","short"])
async def test_ml_synthetic_contract_both_sides_and_engine_owned_timeout(side):
    portfolio,arms,spec,events=make_pair();now=time.perf_counter_ns()
    session=SimpleNamespace(instrument=spec,book_is_fresh=lambda:True,deep_book_is_fresh=lambda:True,
        orderbook=OrderBook(bids=[(99.99,100)],asks=[(100,100)]))
    adapter=ResearchMLAdapter(metadata(),portfolio,portfolio.emit)
    source=SnapshotRef("r","BTCUSDT",1,1,1000,now,"perf_counter",FEATURE_SCHEMA)
    forecast=ImpulseForecast(source,metadata()["model_version"],metadata()["policy_version"],side,30000,now+1,now+1_000_000_000,.6,.2,.2)
    assert adapter.accept(forecast,source,session,now+2,100)[0]
    tasks=[asyncio.create_task(portfolio.dispatch(n)) for n in arms];await asyncio.sleep(.01)
    for arm in arms.values():
        pos=arm.broker.positions["BTCUSDT"]
        assert pos.strategy=="trend_impulse_ml" and pos.quantity>0
        intent=next(iter(arm.intents.values()))
        assert arm.desired(intent,session.orderbook,now_ns=now+30_000_000_001)[0][0]=="timeout"
        # Losing the worker cannot remove a stop managed by the parent.
        bad=OrderBook(bids=[(98,100)],asks=[(98.01,100)]) if side=="long" else OrderBook(bids=[(102,100)],asks=[(102.01,100)])
        assert arm.desired(intent,bad,now_ns=now+10_000_000)[0][0]=="stop"
    for n in arms:portfolio.queues[n].put_nowait(None)
    await asyncio.gather(*tasks)


@pytest.mark.parametrize("mutation,expected",[("abstain","probability_abstention"),("stale","forecast_expired"),("schema","feature_schema_mismatch"),("model","model_version_mismatch"),("drift","entry_drift"),("book","book_unhealthy")])
def test_ml_rejects_without_reservation(mutation,expected):
    portfolio,arms,spec,events=make_pair();now=time.perf_counter_ns();m=metadata()
    source=SnapshotRef("r","BTCUSDT",1,1,1000,now,"perf_counter",FEATURE_SCHEMA)
    f=ImpulseForecast(source,m["model_version"],m["policy_version"],"long",30000,now+1,now+1_000_000_000,.6,.2,.2)
    current=source;quote=100;at=now+2
    session=SimpleNamespace(instrument=spec,book_is_fresh=lambda:mutation!="book",deep_book_is_fresh=lambda:True,
        orderbook=OrderBook(bids=[(99.99,100)],asks=[(100,100)]))
    if mutation=="abstain":f=replace(f,p_target_first=.54,p_stop_first=.26)
    if mutation=="stale":at=now+1_000_000_001
    if mutation=="schema":f=replace(f,source=replace(source,feature_schema="wrong"))
    if mutation=="model":f=replace(f,model_version="wrong")
    if mutation=="drift":quote=90
    ok,reasons=ResearchMLAdapter(m,portfolio,portfolio.emit).accept(f,current,session,at,quote)
    assert not ok and expected in reasons and not portfolio.reservations


def test_market_handler_uses_original_receipt_and_transport_invalidation(tmp_path):
    from scalp_bot.recorder import SessionRecorder
    async def check():
        portfolio,arms,spec,events=make_pair();cfg=settings(tmp_path)
        engine=PairedEngine(cfg,portfolio,None,None,rest_client=_DeniedRest(),recorder=SessionRecorder(str(tmp_path)),configure_observability=False)
        engine.sessions["BTCUSDT"]=ActiveSymbolSession("BTCUSDT",clock=engine.clock)
        handler,fast,deep=engine._market_handler("BTCUSDT")
        from scalp_bot.bybit import MarketMessage
        receipt=time.perf_counter_ns()-1_000_000
        msg=MarketMessage(topic="orderbook.50.BTCUSDT",type="snapshot",ts=int(time.time()*1000),cts=int(time.time()*1000),
            data={"s":"BTCUSDT","b":[["99","10"]],"a":[["101","10"]],"u":1,"seq":1},
            receipt_mono_ns=receipt,receipt_wall_ns=time.time_ns())
        await handler(msg)
        assert engine.source["BTCUSDT"][1]==receipt
        assert engine.sessions["BTCUSDT"].book_synced
        engine._invalidate_transport("BTCUSDT",dict(phase="fault",topics=["orderbook.50.BTCUSDT"]),fast,deep)
        assert not engine.sessions["BTCUSDT"].book_synced
        assert "BTCUSDT" not in portfolio.books and "BTCUSDT" not in engine.covered_since
        await engine.close()
    asyncio.run(check())


def test_environment_scrub_and_stop_signal(monkeypatch):
    import os,signal
    monkeypatch.setenv("DEMO_API_KEY","SENTINEL_PRIVATE")
    class Worker:
        def start(self):assert "DEMO_API_KEY" not in os.environ
    start_worker_without_secrets(Worker());assert os.environ["DEMO_API_KEY"]=="SENTINEL_PRIVATE"
    event=asyncio.Event();old=signal.getsignal(signal.SIGINT);restore=install_stop_signals(event)
    try:signal.getsignal(signal.SIGINT)(signal.SIGINT,None);assert event.is_set()
    finally:restore()
    assert signal.getsignal(signal.SIGINT)==old


async def test_queue_pressure_is_atomic_and_stop_cancels_unsent():
    portfolio,arms,spec,events=make_pair(capacity=1)
    portfolio.queues["demo"].put_nowait("occupied")
    assert not portfolio.admit(Intent.freeze("r",plan(),1,1),spec)[0]
    assert portfolio.queues["paper"].empty() and not portfolio.reservations
    portfolio.queues["demo"].get_nowait()
    i=Intent.freeze("r",plan(),2,2);assert portfolio.admit(i,spec)[0]
    portfolio.halt("operator_stop")
    tasks=[asyncio.create_task(portfolio.dispatch(n)) for n in arms]
    await asyncio.sleep(.01);portfolio.release_reconciled()
    assert not any(a.venue.orders for a in arms.values())
    for n in arms:portfolio.queues[n].put_nowait(None)
    await asyncio.gather(*tasks)


async def test_private_foreign_facts_and_reconciliation_gap():
    rest=MockRest();venue=DemoVenue(rest,lambda *a:None,lambda *a:None)
    with pytest.raises(SafetyError):await venue.message({"topic":"execution","data":[execution(command())]})
    rest.rows=[{"orderLinkId":"FOREIGN"}]
    with pytest.raises(SafetyError):await venue.reconcile()


async def test_slow_demo_dispatch_does_not_block_paper_or_event_loop():
    portfolio,arms,spec,events=make_pair();gate=asyncio.Event();original=arms["demo"].venue.submit
    async def slow(c):await gate.wait();return await original(c)
    arms["demo"].venue.submit=slow
    intent=Intent.freeze("r",plan(),1,time.perf_counter_ns());portfolio.admit(intent,spec)
    tasks=[asyncio.create_task(portfolio.dispatch(n)) for n in arms]
    await asyncio.sleep(.01)
    assert arms["paper"].remaining("BTCUSDT")==1 and arms["demo"].remaining("BTCUSDT")==0
    assert ("demo",intent.pair_id) in portfolio.dispatching
    gate.set();await asyncio.sleep(.01)
    for n in arms:portfolio.queues[n].put_nowait(None)
    await asyncio.gather(*tasks)


@pytest.mark.parametrize("fault",[None,"uid","permissions","account","orders","positions"])
async def test_connected_preflight_is_read_only_and_fail_closed(fault):
    class ReadOnly:
        write_enabled=False
        def __init__(self):self.calls=[]
        async def request(self,method,path,params=None):
            self.calls.append((method,path))
            assert method=="GET"
            if path.endswith("query-api"):
                return dict(userID="wrong" if fault=="uid" else "123",readOnly=0,
                    permissions={"ContractTrade":[] if fault=="permissions" else ["Order","Position"]})
            if path.endswith("info"):return dict(unifiedMarginStatus=1 if fault=="account" else 6,marginMode="REGULAR_MARGIN")
            return {"list":[]}
        async def pages(self,path,params=None):
            self.calls.append(("GET",path))
            if path.endswith("realtime") and fault=="orders":return [{"orderLinkId":"foreign"}]
            if path.endswith("position/list") and fault=="positions":return [dict(size="1",positionIdx=0)]
            return []
    client=ReadOnly();cred=Credentials("fake","secret","123")
    if fault:
        with pytest.raises(SafetyError):await connected_preflight(client,None,cred)
    else:
        result=await connected_preflight(client,None,cred)
        assert result["orders_sent"]==0 and result["masked_uid"]=="***123"
        assert "secret" not in json.dumps(result)
    assert all(m=="GET" for m,p in client.calls)


def test_existing_ready_rule_priority_is_preserved():
    portfolio,arms,spec,events=make_pair();now=time.perf_counter_ns();m=metadata()
    source=SnapshotRef("r","BTCUSDT",1,1,1000,now,"perf_counter",FEATURE_SCHEMA)
    f=ImpulseForecast(source,m["model_version"],m["policy_version"],"long",30000,now+1,now+1_000_000_000,.6,.2,.2)
    session=SimpleNamespace(instrument=spec,book_is_fresh=lambda:True,deep_book_is_fresh=lambda:True,
        orderbook=OrderBook(bids=[(99.99,100)],asks=[(100,100)]))
    ok,reasons=ResearchMLAdapter(m,portfolio,portfolio.emit).accept(f,source,session,now+2,100,rule_ready=True)
    assert not ok and "ordinary_ready_priority" in reasons and not portfolio.reservations


@pytest.mark.parametrize("scope,path",[
    (("linear","USDC"),"/v5/order/realtime"),
    (("linear","USDC"),"/v5/position/list"),
    (("inverse",None),"/v5/position/list"),
    (("option",None),"/v5/position/list"),
    (("spot",None),"/v5/order/realtime"),
])
@pytest.mark.parametrize("phase",["preflight","gap_reconciliation"])
async def test_foreign_account_scopes_are_never_overlooked(scope,path,phase):
    class ForeignAccount:
        write_enabled=False
        def __init__(self):self.writes=[]
        async def request(self,method,endpoint,params=None):
            assert method=="GET"
            if endpoint.endswith("query-api"):
                return dict(userID="123",readOnly=0,permissions={"ContractTrade":["Order","Position"]})
            if endpoint.endswith("info"):return dict(unifiedMarginStatus=6)
            return {"list":[]}
        async def pages(self,endpoint,params):
            if (params.get("category"),params.get("settleCoin"))==scope and endpoint==path:
                return [dict(orderLinkId="FOREIGN",symbol="FOREIGN",size="1",positionIdx=0)]
            return []
    client=ForeignAccount()
    with pytest.raises(SafetyError,match="foreign|pre-existing"):
        if phase=="preflight":await connected_preflight(client,None,Credentials("fake","secret","123"))
        else:await DemoVenue(client,lambda *a:None,lambda *a:None).reconcile()


async def test_private_foreign_linear_position_is_rejected_immediately():
    venue=DemoVenue(MockRest(),lambda *a:None,lambda *a:None)
    with pytest.raises(SafetyError,match="foreign"):
        await venue.message(dict(topic="position",data=[dict(category="linear",symbol="BTCPERP",size="1",positionIdx=0)]))
