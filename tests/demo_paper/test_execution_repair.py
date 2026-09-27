"""Regressions from the owner's ZEC Demo run, without network or new policy."""
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import time
import pytest
from .test_contracts import command, execution, order_row, MockRest
from scalp_bot.demo_paper.contracts import Command, Intent, Order, SafetyError
from scalp_bot.demo_paper.portfolio import Arm, PairedPortfolio
from scalp_bot.demo_paper.preflight import settings
from scalp_bot.demo_paper.transport import DemoReject
from scalp_bot.demo_paper.venues import DemoVenue, PaperVenue
from scalp_bot.domain import OrderBook
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.runtime_clock import SystemRuntimeClock

FIXTURE=json.loads((Path(__file__).parent/"fixtures/zec_protection_failure.json").read_text())

@pytest.mark.parametrize("index",[0,1])
async def test_recorded_zec_partial_and_target_keep_exact_quantities(tmp_path,index):
    case=FIXTURE["pairs"][index];cfg=settings(tmp_path)
    arm=Arm("demo",cfg,SystemRuntimeClock(),lambda *a:None)
    arm.venue=PaperVenue(cfg,lambda *a:None,arm.fill);arm.venue.remaining=arm.remaining
    intent=Intent(case["commands"][0]["pair_id"],json.dumps(case["plan"]),1,time.perf_counter_ns(),"rule")
    spec=InstrumentSpec(**case["spec"]);arm.intents[intent.pair_id]=intent;arm.specs[intent.pair_id]=spec
    entry=Command(**case["commands"][0]);arm.venue.register(entry)
    fill=next(f for f in case["fills"] if f["orderLinkId"]==entry.link_id)
    arm.venue.execution(fill);arm.venue.order_update(order_row(entry,entry.qty))
    book=OrderBook(bids=[(float(fill["execPrice"])-.01,100)],asks=[(float(fill["execPrice"]),100)])
    arm.venue.market(entry.symbol,book)
    portfolio=PairedPortfolio("r",cfg,{"demo":arm},lambda *a:None)
    desired=arm.desired(intent,book,now_ns=time.perf_counter_ns())
    target=next(d for d in desired if d[0]=="target")
    assert target[1]==Decimal("1.74" if index==0 else "1.75")
    await portfolio.synchronize(arm,intent,desired)
    assert all(Decimal(o.command.qty)%Decimal(str(spec.qty_step))==0 for o in arm.venue.orders.values())
    # An unchanged plan must not cancel and recreate its limits.
    count=len(arm.venue.orders)
    for _ in range(3):await portfolio.synchronize(arm,intent,arm.desired(intent,book,now_ns=time.perf_counter_ns()))
    assert len(arm.venue.orders)==count
    if index==0:
        partial=next(o for o in arm.venue.orders.values() if o.command.reason=="partial_take")
        original=next(c for c in case["commands"] if c["reason"]=="partial_take")
        f=next(f for f in case["fills"] if f["orderLinkId"]==original["link_id"])
        f={**f,"orderLinkId":partial.command.link_id}
        arm.venue.execution(f);arm.venue.order_update(order_row(partial.command,".74"))
        assert arm.remaining(entry.symbol)==Decimal("1.74")
        book=OrderBook(bids=[(float(f["execPrice"])-.01,100)],asks=[(float(f["execPrice"]),100)])
        arm.venue.market(entry.symbol,book)
        for _ in range(4):await portfolio.synchronize(arm,intent,arm.desired(intent,book,now_ns=time.perf_counter_ns()))
        runners=[o for o in arm.venue.orders.values() if o.command.reason=="runner_target"]
        assert len(runners)==1 and runners[0].command.qty=="1.74"
        assert runners[0].status=="New"
        for _ in range(3):
            await portfolio.synchronize(arm,intent,arm.desired(intent,book,now_ns=time.perf_counter_ns(),stop_reason="operator_stop"))
        assert arm.remaining(entry.symbol)==0 and not arm.broker.positions
        closing=[o for o in arm.venue.orders.values() if o.command.reason=="operator_stop"]
        assert len(closing)==1 and closing[0].filled==Decimal("1.74")
    # A physical non-step quantity is rejected, never rounded into a legal order.
    with pytest.raises(SafetyError,match="instrument step"):
        arm.command("r",intent,qty="1.7500000000000002",mode="Market",reduce=True)

@pytest.mark.parametrize("code,certain",[(10001,True),(10014,False),(10000,False),(110072,False)])
async def test_create_rejection_provenance_survives_forced_reconciliation(code,certain):
    class Reject(MockRest):
        async def request(self,*args):raise DemoReject(code)
    rest=Reject();venue=DemoVenue(rest,lambda *a:None,lambda *a:None)
    o=await venue.submit(command())
    await venue.reconcile(force=True);await venue.reconcile(force=True)
    assert o.terminal is certain and o.confirmed is certain
    assert o.unknown is (not certain)
    assert o.create_rejected is certain

async def test_final_reconcile_resumes_completed_checks_after_cancellation():
    class Exchange(MockRest):
        def __init__(self):super().__init__();self.lookups=[];self.block_second=True
        async def pages(self,path,params):
            key=params.get("orderLinkId")
            if not key:return []
            self.lookups.append((path,key))
            if key==c2.link_id and self.block_second:
                self.block_second=False;raise asyncio.CancelledError
            c=c1 if key==c1.link_id else c2
            if path.endswith("execution/list"):return []
            return [order_row(c,"0","Cancelled")]
    c1=command(revision=1);c2=command(revision=2);rest=Exchange()
    venue=DemoVenue(rest,lambda *a:None,lambda *a:None)
    for c in (c1,c2):
        o=venue.register(c);o.update_order(order_row(c,"0","Cancelled"),1);o.confirmed=True
    with pytest.raises(asyncio.CancelledError):await venue.reconcile(force=True)
    await venue.reconcile(force=True)
    assert sum(key==c1.link_id for _,key in rest.lookups)==2
    assert sum(key==c2.link_id for _,key in rest.lookups)==3
    # New facts invalidate the cache; unchanged terminal orders do not starve
    # the all-scope position/open-order checks on every bounded final pass.
    venue.orders[c1.link_id].updated_ms+=1
    await venue.reconcile(force=True)
    assert sum(key==c1.link_id for _,key in rest.lookups)==4
