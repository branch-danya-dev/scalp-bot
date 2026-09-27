"""Whole lifecycle through real coordinator/venue; every network boundary mocked."""
import asyncio
from decimal import Decimal
from pathlib import Path
import time
import pytest
from .test_contracts import plan, order_row, execution
from .test_integration import metadata
from scalp_bot.demo_paper.contracts import Intent
from scalp_bot.demo_paper.preflight import settings
from scalp_bot.demo_paper.runtime import Session
from scalp_bot.demo_paper.transport import Credentials
from scalp_bot.domain import OrderBook
from scalp_bot.engine import ActiveSymbolSession
from scalp_bot.instrument import InstrumentSpec

class Exchange:
    def __init__(self):self.commands={};self.fills={};self.position=Decimal(0);self.calls=[];self.serial=0
    async def request(self,method,path,params):
        self.calls.append((method,path,dict(params)))
        key=params.get("orderLinkId");now=str(int(time.time()*1000));self.serial+=1
        if path=="/v5/order/create":
            qty=Decimal(params["qty"]);side=params["side"]
            row=dict(orderLinkId=key,orderId=str(self.serial),symbol=params["symbol"],side=side,
                orderStatus="New",cumExecQty="0",qty=str(qty),price=params.get("price","0"),updatedTime=now)
            self.commands[key]=row;self.fills[key]=[]
            if params["orderType"]=="Market":
                if params["reduceOnly"]:qty=min(qty,abs(self.position))
                for fraction in (Decimal(".4"),Decimal(".6")):
                    q=qty*fraction
                    if q:
                        self.fills[key].append(dict(orderLinkId=key,symbol=params["symbol"],side=side,
                            execType="Trade",execId=f"{key}-{fraction}",execQty=str(q),execPrice="100",
                            execFee=str(q*Decimal(".055")),feeCurrency="USDT",isMaker=False,execTime=now))
                        self.position+=q*(1 if side=="Buy" else -1)
                row.update(orderStatus="Filled",cumExecQty=str(qty))
            return {"orderId":row["orderId"]}
        if path=="/v5/order/cancel":self.commands[key].update(orderStatus="Cancelled",updatedTime=now);return {}
        raise AssertionError((method,path))
    async def pages(self,path,params):
        if params.get("category","linear")!="linear" or params.get("settleCoin","USDT")!="USDT":return []
        if path=="/v5/account/transaction-log":return []
        if path=="/v5/position/list":return [dict(symbol="BTCUSDT",side="Buy" if self.position>=0 else "Sell",size=str(abs(self.position)),positionIdx=0)]
        key=params.get("orderLinkId")
        if path=="/v5/execution/list":return self.fills.get(key,[])
        if path in ("/v5/order/realtime","/v5/order/history"):
            return [self.commands[key]] if key in self.commands else [r for r in self.commands.values() if r["orderStatus"]=="New"]
        raise AssertionError(path)
    async def close(self):pass

class Worker:
    ready=True;failed=None;inflight=None;latest={};info={}
    def start(self):pass
    def close(self):pass
    def poll(self):return []


@pytest.mark.parametrize("initial_clock_rejected",[False,True])
async def test_full_mock_start_partial_cancel_stop_reconcile_report(tmp_path,monkeypatch,initial_clock_rejected):
    import scalp_bot.demo_paper.runtime as runtime
    root=tmp_path/"synthetic-session";cfg=settings(root/"capture")
    session=Session(cfg,metadata(),tmp_path,Credentials("FAKE_KEY","FAKE_SECRET","123"),{},root)
    exchange=Exchange();await session.rest.close();session.rest=exchange
    session.arms["demo"].venue.rest=exchange;session.worker=Worker();session.engine.worker=session.worker
    async def preflight(*args):return {"status":"SYNTHETIC_NO_NETWORK"}
    async def private(credentials,on_message,on_gap,stop):
        await on_gap("reconnected");await stop.wait()
    monkeypatch.setattr(runtime,"connected_preflight",preflight)
    monkeypatch.setattr(runtime,"private_stream",private)
    spec=InstrumentSpec("BTCUSDT","Trading",.01,.001,.001,5,100,100,480,100)
    book=OrderBook(bids=[(99.99,100)],asks=[(100,100)])
    async def bootstrap():
        mono=time.perf_counter_ns()/1e9;wall=time.time()*1000
        assert session.engine.market_clock.synchronize(server_ms=wall,
            sent_mono=mono-(.4595151 if initial_clock_rejected else .01),
            received_mono=mono,received_wall_ms=wall) is (not initial_clock_rejected)
        market=ActiveSymbolSession("BTCUSDT",clock=session.clock)
        market.orderbook=book;market.last_book_at=time.time();market.book_synced=True;market.instrument=spec
        session.engine.sessions["BTCUSDT"]=market
        session.portfolio.books["BTCUSDT"]=book;session.arms["paper"].venue.market("BTCUSDT",book)
        async def submit_then_stop():
            if initial_clock_rejected:
                await asyncio.sleep(.02)
                session.engine._arbitrate_once()
                assert session.portfolio.stop_reason is None and not session.portfolio.accepting
                assert not exchange.commands
                mono=time.perf_counter_ns()/1e9;wall=time.time()*1000
                assert session.engine.market_clock.synchronize(server_ms=wall,
                    sent_mono=mono-.2380371,received_mono=mono,received_wall_ms=wall)
            while not session.portfolio.accepting:await asyncio.sleep(.001)
            intent=Intent.freeze(session.run,plan(),1,time.perf_counter_ns())
            assert session.portfolio.admit(intent,spec)[0]
            until=time.monotonic()+3
            while time.monotonic()<until and not all(a.remaining("BTCUSDT")==1 for a in session.arms.values()):await asyncio.sleep(.005)
            assert all(a.remaining("BTCUSDT")==1 for a in session.arms.values())
            # Both independently book their fills, with actual Demo partials.
            assert len(session.arms["demo"].bookings)==2 and len(session.arms["paper"].bookings)==1
            session.stop.set()
        session.tasks.append(asyncio.create_task(submit_then_stop()))
    session.engine.start=bootstrap
    original_reconcile=session.reconcile
    async def checked_reconcile(*,final=False):
        if final:assert session.account_task is None or session.account_task.done()
        return await original_reconcile(final=final)
    session.reconcile=checked_reconcile
    result=await asyncio.wait_for(session.run_session(),10)
    assert result["positions_reconciled"] and exchange.position==0
    assert result["status"]=="INCOMPLETE" and result["reason"]=="operator_stop"
    assert result["ml_status"]=="ML_TRADING_NOT_TESTED"
    assert all(not a.broker.positions for a in session.arms.values())
    assert (root/"paired_orders.csv").is_file() and (root/"manifest.json").is_file()
    assert "FAKE_SECRET" not in (root/"experiment-events.jsonl").read_text()
    creates=[p for m,path,p in exchange.calls if path.endswith("create")]
    assert sum(not p["reduceOnly"] for p in creates)==1
    assert all(p["reduceOnly"] for p in creates[1:])
    assert result["recorder_health"]["droppedRows"]==0
    starts=[e for e in session.events if e["event"]=="start"]
    assert len(starts)==1
    assert starts[0]["payload"]["deadline_ns"]-starts[0]["payload"]["start_ns"]==3_600_000_000_000
    if initial_clock_rejected:
        readiness=[e["payload"] for e in session.events if e["event"]=="entry_readiness"]
        assert readiness[0]["reason"]=="clock:sync_rtt_exceeded"
        assert any(r["accepting"] for r in readiness[1:])


async def test_signed_funding_dedup_and_unconfirmed_boundary(tmp_path):
    from types import SimpleNamespace
    from .test_contracts import make_pair
    from scalp_bot.demo_paper.contracts import Intent
    portfolio,arms,spec,events=make_pair();intent=Intent.freeze("r",plan(),1,1)
    portfolio.admit(intent,spec);pair=intent.pair_id
    arms["demo"].bookings.append(dict(pair_id=pair,execTime="1000",execQty="1",side="Buy"))
    class Rest:
        async def pages(self,*args):
            return [dict(type="SETTLEMENT",id="f1",currency="USDT",symbol="BTCUSDT",transactionTime="2000",funding="-.1")]
    fake=SimpleNamespace(start_ms=500,rest=Rest(),arms=arms,funding_seen=set(),observed_funding=set(),
        expected_funding={(pair,"BTCUSDT",2000)},emit=lambda *a:None)
    assert not Session.funding_complete(fake)
    await Session.funding(fake);await Session.funding(fake)
    assert arms["demo"].broker.balance==999.9 and Session.funding_complete(fake)
    fake.expected_funding.add((pair,"BTCUSDT",10000))
    assert not Session.funding_complete(fake)
