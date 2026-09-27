"""Money completeness and monotonic stage boundaries; no network."""
import asyncio
import csv
from types import SimpleNamespace
import time
import httpx
import pytest
from .test_contracts import command, make_pair, plan
from scalp_bot.demo_paper.contracts import Intent
from scalp_bot.demo_paper.runtime import Session
from scalp_bot.demo_paper.transport import Credentials, DemoRest
from scalp_bot.demo_paper.venues import DemoVenue
from scalp_bot.demo_paper.report import write_report


async def test_private_send_timestamp_is_after_transport_queue():
    received=[]
    def response(request):
        received.append(time.perf_counter_ns())
        return httpx.Response(200,json={"retCode":0,"result":{"orderId":"mock"}})
    rest=DemoRest(Credentials("fake","secret","123"),enabled=True,transport=httpx.MockTransport(response))
    venue=DemoVenue(rest,lambda *a:None,lambda *a:None)
    order=venue.register(command(reduce_only=True,reason="stop"));order.sent_ns=time.perf_counter_ns()
    session=SimpleNamespace(arms={"demo":SimpleNamespace(venue=venue)},emit=lambda *a:None)
    rest.before_write=lambda path,params:Session.before_write(session,path,params)
    rest.write_enabled=True
    await rest._lock.acquire()
    task=asyncio.create_task(rest.request("POST","/v5/order/create",order.command.payload()))
    try:
        await asyncio.sleep(0)
        queued_until=time.perf_counter_ns();rest._lock.release()
        await task
        assert queued_until<order.sent_ns<=received[0]
    finally:
        if rest._lock.locked():rest._lock.release()
        if not task.done():task.cancel()
        await rest.close()


async def test_missing_funding_is_blank_separately_from_observed_cash(tmp_path):
    portfolio,arms,spec,events=make_pair();intent=Intent.freeze("r",plan(),1,time.perf_counter_ns())
    assert portfolio.admit(intent,spec)[0]
    for name,arm in arms.items():
        portfolio.queues[name].get_nowait()
        await arm.venue.submit(arm.command("r",intent,qty=1,mode="Market"))
        await arm.venue.submit(arm.command("r",intent,qty=1,mode="Market",reduce=True,reason="stop"))
    portfolio.release_reconciled()
    assert intent.pair_id in portfolio.completed
    session=SimpleNamespace(output=tmp_path,events=[],arms=arms,portfolio=portfolio,config=portfolio.config,
        expected_funding={(intent.pair_id,"BTCUSDT",2000)},observed_funding=set(),funding_complete=lambda:False,
        run="synthetic",preflight={},start_ns=time.perf_counter_ns(),loop_lateness_ms=[],
        recorder=SimpleNamespace(health=lambda:{}),public=SimpleNamespace(spec_evidence={}))
    result=write_report(session,False,"unconfirmed_funding")
    with (tmp_path/"paired_orders.csv").open() as file:row=next(csv.DictReader(file))
    assert row["demo_funding"]==""
    assert row["demo_observed_funding"]=="0" and row["demo_funding_known"]=="False"
    assert row["demo_net"]=="" and result["comparison"]["final_net"]["demo"] is None
