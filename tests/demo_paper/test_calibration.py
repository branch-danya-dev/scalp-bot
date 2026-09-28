from dataclasses import replace
import pytest
from scalp_bot.demo_paper.calibration import DemoExecutionCalibration
from scalp_bot.demo_paper.contracts import Intent, Command, SafetyError
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.domain import TradePlan, Side


def fixture():
    plan=TradePlan("AAAUSDT","level_breakout",Side.LONG,100,100,99,102,100,1,1,2,0,2,1,2,0,"s",quantity=1)
    intent=Intent.freeze("run",plan,1,1)
    spec=InstrumentSpec("AAAUSDT","Trading",.01,.001,.001,5,10000,10000,480,10)
    cal=DemoExecutionCalibration(run_id="run",account_hash="a"*64,instrument_hash="b"*64)
    cal.freeze(intent,spec)
    return cal,intent


def fill(command,ident,qty):
    return dict(orderLinkId=command.link_id,symbol="AAAUSDT",side="Buy",execType="Trade",
        execId=ident,execQty=str(qty),execPrice="100.01",execFee="0.01",feeCurrency="USDT",isMaker=False)


def test_independent_partial_fills_actual_costs_and_no_cancel_ack_reconciliation():
    cal,intent=fixture()
    commands={arm:Command.make(arm,intent.pair_id,"AAAUSDT","Buy",1,"Market",None,False,"entry",1,10)
        for arm in ("paper","demo")}
    for arm,c in commands.items():
        cal.sent(arm,c,20);cal.ack(arm,c.link_id,30)
        cal.execution(arm,fill(c,"one",.4),40)
    cal.execution("demo",fill(commands["demo"],"one",.4),40)
    assert len(cal.fills)==2
    cal.operation("demo",commands["demo"].link_id,"cancel_ack",50)
    assert not cal.reconcile("demo",account_hash="a"*64,instrument_hash="b"*64,
        foreign_activity=False,unknown_exposure=False,positions_flat=True)
    result=cal.report()
    assert result["ledgers"]["demo"][0]["remaining"]=="0.6"
    assert result["timing"]["demo"]["order_to_final_fill_ms"]["count"]==0
    assert result["liveQualification"]=="NOT_TESTED" and not result["executionAuthorized"]


def test_frozen_geometry_foreign_activity_unknown_fee_and_precision_are_rejected():
    cal,intent=fixture()
    c=Command.make("demo",intent.pair_id,"AAAUSDT","Buy",2,"Market",None,False,"entry",1,10)
    with pytest.raises(SafetyError,match="frozen"):cal.sent("demo",c,20)
    with pytest.raises(SafetyError,match="foreign"):cal.execution("demo",fill(c,"one",1),40)
    assert cal.report()["status"]=="NOT_MET"


def test_entry_mode_cannot_differ_from_frozen_plan():
    cal,intent=fixture()
    c=Command.make("demo",intent.pair_id,"AAAUSDT","Buy",1,"PostOnly",99,False,"entry",1,10)
    with pytest.raises(SafetyError,match="geometry"):
        cal.sent("demo",c,20)


def test_empty_ledgers_cannot_complete_contract():
    cal,intent=fixture()
    for arm in ("paper","demo"):
        assert not cal.reconcile(arm,account_hash="a"*64,instrument_hash="b"*64,
            foreign_activity=False,unknown_exposure=False,positions_flat=True)
    assert cal.report()["status"] == "INCONCLUSIVE"
