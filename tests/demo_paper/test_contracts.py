import asyncio
from dataclasses import replace
from decimal import Decimal
import time
import httpx
import pytest
from scalp_bot.config import Settings
from scalp_bot.domain import Side, TradePlan, OrderBook
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.runtime_clock import SystemRuntimeClock
from scalp_bot.demo_paper.contracts import Command, Intent, Order, SafetyError
from scalp_bot.demo_paper.transport import Credentials, DemoRest, NoRedirectConnect
from scalp_bot.demo_paper.venues import DemoVenue, PaperVenue
from scalp_bot.demo_paper.portfolio import Arm, PairedPortfolio


def plan(side=Side.LONG):
    return TradePlan("BTCUSDT","trend_structure",side,100,100,99 if side==Side.LONG else 101,
        103 if side==Side.LONG else 97,100,1,1,3,.11,2.89,1.11,2.6,0,"setup",quantity=1,
        strategy_details={"allowRunner":False})


def command(**kw):
    values=dict(run="run",pair="pair",symbol="BTCUSDT",side="Buy",qty=1,mode="Market",price=None,
                reduce_only=False,reason="entry",revision=1,now_ns=time.perf_counter_ns())
    values.update(kw);return Command.make(**values)


def execution(c,ident="e1",qty="1",price="100",fee=".055"):
    return dict(orderLinkId=c.link_id,symbol=c.symbol,side=c.side,execId=ident,execType="Trade",
                execQty=qty,execPrice=price,execFee=fee,feeCurrency="USDT",execTime="1000",isMaker=False)


def order_row(c,qty="1",status="Filled",stamp="1000"):
    return dict(orderLinkId=c.link_id,symbol=c.symbol,side=c.side,orderId="oid",orderStatus=status,
                cumExecQty=qty,updatedTime=stamp)


def test_immutable_intent_and_api_identity():
    p=plan();i=Intent.freeze("run",p,10,20);p.stop=1
    assert i.plan().stop==99
    assert len(command().link_id)==35
    assert command().link_id==command().link_id
    with pytest.raises(SafetyError):replace(command(),qty="NaN")
    with pytest.raises(SafetyError):replace(command(),symbol="BTCUSD")


def test_ack_not_fill_late_duplicate_and_fees():
    c=command();o=Order(c);o.update_order(order_row(c),50)
    assert not o.terminal
    assert o.fill(execution(c,qty=".4",fee=".02"),60)
    assert not o.fill(execution(c,qty=".4",fee=".02"),61)
    assert o.fill(execution(c,"e2",".6","101",".03"),70)
    assert o.terminal and o.average==Decimal("100.6") and o.fees==Decimal(".05")
    o.update_order(order_row(c,"0","New","900"),80)
    assert o.terminal
    with pytest.raises(SafetyError):o.fill(execution(c,qty=".5"),90)


@pytest.mark.parametrize("field,value",[("feeCurrency","BTC"),("feeCurrency",None),("side","Sell"),("execQty","2"),("execPrice","nan")])
def test_invalid_private_fact(field,value):
    c=command();row=execution(c);row[field]=value
    with pytest.raises(SafetyError):Order(c).fill(row,2)


async def test_private_hosts_disabled_redirect_and_no_order_preflight():
    cred=Credentials("FAKE_KEY","FAKE_SECRET","123")
    assert "FAKE" not in repr(cred)
    with pytest.raises(SafetyError):DemoRest(cred)
    with pytest.raises(SafetyError):DemoRest(cred,enabled=True,base_url="https://api.bybit.com")
    seen=[]
    def reply(request):
        seen.append(request)
        return httpx.Response(302,headers={"location":"https://api.bybit.com/v5/order/create"})
    rest=DemoRest(cred,enabled=True,transport=httpx.MockTransport(reply))
    try:
        with pytest.raises(SafetyError):await rest.request("POST","/v5/order/create",{})
        with pytest.raises(SafetyError):await rest.request("GET","/v5/order/realtime",{})
        assert len(seen)==1 and seen[0].url.host=="api-demo.bybit.com"
        assert isinstance(NoRedirectConnect.process_redirect(None,Exception()),SafetyError)
        rest.write_enabled=True
        for endpoint in ("/v5/order/cancel-all","/v5/position/set-leverage","/v5/account/demo-apply-money"):
            with pytest.raises(SafetyError):await rest.request("POST",endpoint,{})
    finally:await rest.close()


class MockRest:
    def __init__(self):self.calls=[];self.rows=[];self.fills=[];self.positions=[];self.timeout=False
    async def request(self,method,path,params):
        self.calls.append((method,path,params))
        if self.timeout:raise SafetyError("outcome unknown")
        return {"orderId":"oid"}
    async def pages(self,path,params):
        if params.get("category","linear")!="linear" or params.get("settleCoin","USDT")!="USDT":return []
        if path=="/v5/execution/list":return self.fills
        if path=="/v5/position/list":return self.positions
        return self.rows


async def test_uncertain_create_resolves_same_id_and_cancel_racing_fill_once():
    rest=MockRest();c=command();rest.timeout=True
    rest.rows=[order_row(c,".4","PartiallyFilled")];rest.fills=[execution(c,qty=".4",fee=".022")]
    fills=[];venue=DemoVenue(rest,lambda *a:None,lambda *a:fills.append(a))
    o=await venue.submit(c)
    assert o.filled==Decimal(".4") and not o.unknown and len(rest.calls)==1
    rest.rows=[order_row(c,"1","Filled","1001")];rest.fills.append(execution(c,"e2",".6"))
    await venue.cancel(o)
    assert o.terminal and len(fills)==2
    await venue.reconcile_order(o)
    assert len(fills)==2 and sum(path.endswith("create") for _,path,_ in rest.calls)==1


async def test_unresolved_create_is_not_retried():
    rest=MockRest();rest.timeout=True;venue=DemoVenue(rest,lambda *a:None,lambda *a:None)
    o=await venue.submit(command())
    assert o.unknown and not venue.healthy and not o.terminal and len(rest.calls)==1


async def test_paper_does_not_use_pre_submit_or_pre_amend_tape():
    cfg=Settings(_env_file=None);fills=[];venue=PaperVenue(cfg,lambda *a:None,lambda *a:fills.append(a))
    book=OrderBook(bids=[(99,100)],asks=[(101,100)])
    venue.market("BTCUSDT",book)
    c=command(mode="PostOnly",price=99);o=await venue.submit(c)
    venue.market(c.symbol,book,trade_price=98,trade_notional=1e6,trade_side="Sell",receipt_ns=c.created_ns-1)
    assert not fills
    await venue.amend(o,qty=1,price=98)
    venue.market(c.symbol,book,trade_price=97,trade_notional=1e6,trade_side="Sell",receipt_ns=o.revision_ns-1)
    assert not fills
    venue.market(c.symbol,book,trade_price=97,trade_notional=1e6,trade_side="Sell",receipt_ns=o.revision_ns+1)
    assert len(fills)==1 and o.terminal


def make_pair(capacity=2):
    cfg=Settings(_env_file=None);clock=SystemRuntimeClock();events=[]
    emit=lambda *args:events.append(args)
    arms={n:Arm(n,cfg,clock,emit) for n in ("demo","paper")}
    for a in arms.values():
        a.venue=PaperVenue(cfg,emit,a.fill);a.venue.remaining=a.remaining
    portfolio=PairedPortfolio("run",cfg,arms,emit,capacity=capacity);portfolio.accepting=True
    book=OrderBook(bids=[(99.99,100)],asks=[(100,100)])
    portfolio.books["BTCUSDT"]=book
    for a in arms.values():a.venue.market("BTCUSDT",book)
    spec=InstrumentSpec("BTCUSDT","Trading",.01,.001,.001,5,100,100,480,100)
    return portfolio,arms,spec,events


async def test_atomic_pair_shared_budget_and_independent_fills():
    portfolio,arms,spec,events=make_pair();intent=Intent.freeze("run",plan(),1,time.perf_counter_ns())
    assert portfolio.admit(intent,spec)[0]
    assert not portfolio.admit(Intent.freeze("run",plan(),2,2),spec)[0]
    tasks=[asyncio.create_task(portfolio.dispatch(n)) for n in arms]
    await asyncio.sleep(.01)
    assert all(a.remaining("BTCUSDT")==1 for a in arms.values())
    assert arms["paper"].broker.positions["BTCUSDT"] is not arms["demo"].broker.positions["BTCUSDT"]
    portfolio.halt("duration_elapsed")
    await portfolio.manage_once();portfolio.release_reconciled()
    assert not portfolio.reservations
    assert all(a.broker.balance<1000 for a in arms.values())
    for n in arms:portfolio.queues[n].put_nowait(None)
    await asyncio.gather(*tasks)


async def test_reduce_only_late_partial_cannot_reverse():
    portfolio,arms,spec,events=make_pair();intent=Intent.freeze("run",plan(),1,time.perf_counter_ns())
    portfolio.admit(intent,spec);arm=arms["demo"]
    c=arm.command("run",intent,qty=1,mode="Market");await arm.venue.submit(c)
    close=arm.command("run",intent,qty=2,mode="Market",reduce=True,reason="stop")
    result=await arm.venue.submit(close)
    assert result.filled==1 and not arm.broker.positions
    assert len(arm.broker.closed_trades)==1


async def test_unknown_fee_retains_owned_exposure_for_protection_and_never_known_net():
    portfolio,arms,spec,events=make_pair();i=Intent.freeze("r",plan(),1,time.perf_counter_ns())
    portfolio.admit(i,spec);arm=arms["demo"];rest=MockRest()
    arm.venue=DemoVenue(rest,lambda *a:None,arm.fill)
    c=arm.command("r",i,qty=1,mode="Market")
    arm.venue.register(c)
    row=execution(c);row["feeCurrency"]=None
    arm.venue.execution(row);arm.venue.execution(row)
    assert arm.remaining("BTCUSDT")==1 and not arm.venue.healthy
    assert not arm.venue.orders[c.link_id].fees_known
    assert len(arm.bookings)==1 and arm.bookings[0]["_fee_unknown"]
