from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, StrategyDecision, Trend, Candle, OrderBook
from scalp_bot.risk import RiskEngine
from scalp_bot.trading_policy import reachable_target, participation_quality, rejection_classification, cross_classification
from scalp_bot.trading24h import settings, manifest, duration_elapsed, require_demo_equity, ReadRetryDemoRest, DemoEngine
from scalp_bot.demo_paper.contracts import SafetyError
from scalp_bot.demo_paper.transport import Credentials


def decision(side="long", strategy="level_breakout"):
    return StrategyDecision(strategy, Action(side), [], entry=100, stop=99.7 if side=="long" else 100.3,
        target=110 if side=="long" else 90,
        details={"opportunityTrigger": dict(price=100., expectedImpulsePct=.01, observedAtMs=10000),
                 "marketTargetOnly": True})


@pytest.mark.parametrize("side,entry,expected,spent", [("long",100.6,101.,60), ("short",99.4,99.,60),
    ("long",101.1,101.1,110), ("short",98.9,98.9,110)])
def test_remaining_move_uses_frozen_anchor(side,entry,expected,spent):
    d=decision(side)
    d.details["expectedImpulsePct"] = .30  # Later volatility cannot expand the causal budget.
    target,reason=reachable_target(d,entry,d.target)
    assert target == pytest.approx(expected)
    assert d.details["remainingMove"]["spentBps"] == pytest.approx(spent)
    assert bool(reason) == (spent > 100)


@pytest.mark.parametrize("side,liquidity", [("long",100.7),("short",99.3)])
def test_nearest_reachable_liquidity_caps_target(side,liquidity):
    d=decision(side);d.details["liquidityLadder"]=[dict(price=liquidity)]
    target,reason=reachable_target(d,100.,d.target)
    assert target==liquidity and reason is None


def test_countertrend_short_budget_no_renewal():
    d=decision();d.details.update(rejectionClass="countertrend_reaction",reactionBudget=.4)
    target,_=reachable_target(d,100.2,110)
    assert target == pytest.approx(100.4)
    assert reachable_target(d,100.41,110)[1]=="remaining_move_exhausted"


def test_missing_anchor_fails_closed():
    d=decision();d.details.pop("opportunityTrigger")
    assert reachable_target(d,100,110)[1]=="causal_movement_anchor_missing"


def test_participation_blocks_xpl_like_thin_flow_and_is_scale_invariant():
    flow=dict(baselineReady=True,notional5s=1500.,acceleration=1.5,tradeRateRatio=1.,tradeCount5s=5,
              imbalance5s=-.9,priceMove5sPct=-.001)
    candles=[NS(turnover=120000.,confirmed=True)]*20
    result=participation_quality(flow,candles,NS(volume_pace_ratio=.336),"short",Settings())
    assert not result["confirmed"]
    assert "candle_volume_pace_weak" in result["reasons"]
    assert result["notionalRatio"]==pytest.approx(.15)
    flow["notional5s"]=10000
    assert participation_quality(flow,candles,NS(volume_pace_ratio=1),"short",Settings())["confirmed"]
    flow["notional5s"]*=.01
    assert participation_quality(flow,[NS(turnover=1200.,confirmed=True)]*20,NS(volume_pace_ratio=1),"short",Settings())["confirmed"]


@pytest.mark.parametrize("regime,direction,side,expected",[
    ("bullish_trend",Trend.UP,"long","trend_aligned_rejection"),
    ("bullish_impulse",Trend.UP,"short","countertrend_reaction"),
    ("range",Trend.UP,"short","range_rejection"),
    ("pullback",Trend.DOWN,"long","countertrend_reaction")])
def test_rejection_classification(regime,direction,side,expected):
    context=NS(local_regime=NS(regime=NS(value=regime),direction=direction,parent_direction=direction))
    assert rejection_classification(context,side)==expected


@pytest.mark.parametrize("move,flow,expected",[(2,.5,"aligned"),(-2,-.5,"opposed"),(0,.5,"neutral"),(2,-.5,"neutral")])
def test_cross_venue_requires_fresh_agreement_of_price_and_executions(move,flow,expected):
    row=dict(available=True,ageMs=50,returnsBps={"1000":move},tradeImpulse=flow,tradeVolumeUsd=5000)
    snapshot=dict(venues={v:deepcopy(row) for v in ("binance","okx")})
    assert cross_classification(snapshot,"long",Settings())==expected
    snapshot["venues"]["okx"]["ageMs"]=1501
    assert cross_classification(snapshot,"long",Settings())=="unavailable"
    assert cross_classification({},"long",Settings())=="unavailable"


def test_profile_ignores_environment_and_retains_risk(tmp_path,monkeypatch):
    monkeypatch.setenv("SCALP_START_BALANCE","90000")
    monkeypatch.setenv("SCALP_ENFORCE_SESSION_LOSS_LIMIT","true")
    c=settings(tmp_path);m=manifest(c)
    assert c.paper_run_duration_seconds==86400 and c.start_balance==1000
    assert c.risk_fraction==.005 and c.max_trade_all_in_loss_fraction==.0125
    assert c.max_leverage==10 and c.max_position_leverage==5
    assert c.enforce_net_reward_risk_gate and c.min_net_reward_risk==1.15
    assert c.absolute_min_net_reward_risk==1 and c.enforce_winner_cost_share_gate
    assert not c.enforce_min_net_profit_gate
    assert not any(m["artificialLimits"].values()) and not any(m["mlAuthority"].values())
    assert m["tradeableStrategies"]==["level_breakout","weak_level_rejection","trend_structure"]


def test_exact_monotonic_duration():
    assert not duration_elapsed(55,55+86400*10**9-1)
    assert duration_elapsed(55,55+86400*10**9)


def test_demo_equity_is_actual_and_risk_is_capped(tmp_path):
    report=dict(wallet=dict(list=[dict(coin=[dict(coin="USDT",equity="1000")])]))
    assert require_demo_equity(report)==1000
    report["wallet"]["list"][0]["coin"][0]["equity"]="100000"
    actual=require_demo_equity(report)
    assert actual==100000
    c=settings(tmp_path,equity=actual)
    assert c.start_balance==actual
    assert c.risk_fraction*actual==pytest.approx(5)
    assert c.max_trade_all_in_loss_fraction*actual==pytest.approx(12.5)
    assert c.max_position_leverage*actual==pytest.approx(5000)
    assert c.max_leverage*actual==pytest.approx(10000)
    small=settings(tmp_path,equity=500)
    assert small.risk_fraction*500==pytest.approx(2.5)


@pytest.mark.asyncio
async def test_demo_rest_retries_reads_only(monkeypatch):
    from scalp_bot.demo_paper.transport import DemoRest, DemoReject
    calls=[]
    async def request(self,method,path,params):
        calls.append(method)
        if len(calls)<3:raise DemoReject(10006)
        return {}
    async def sleep(seconds):pass
    monkeypatch.setattr(DemoRest,"request",request)
    monkeypatch.setattr("scalp_bot.trading24h.asyncio.sleep",sleep)
    rest=ReadRetryDemoRest(Credentials("x","y","z"),enabled=True)
    try:
        assert await rest.request("GET","/v5/account/info")=={}
        assert len(calls)==3
        calls.clear()
        with pytest.raises(DemoReject):await rest.request("POST","/v5/order/create")
        assert len(calls)==1
    finally:await rest.close()


def test_reachable_economics_rejects_spent_trade_despite_remote_target(tmp_path):
    c=settings(tmp_path);d=decision();d.details["opportunityTrigger"]["expectedImpulsePct"]=.001
    book=OrderBook(bids=[(100.,10000.)],asks=[(100.01,10000.)])
    result=RiskEngine(c).build_plan("XUSDT",d,1000,book,10000,20)
    assert not result.allowed
    assert d.details["remainingMove"]["reachableTarget"]<=100.1+1e-9


def policy_fixture(tmp_path, direction=Trend.UP, regime="bullish_trend", resistance=None):
    from test_semantic_arbiter import context
    from scalp_bot.domain import TradeTick
    ctx=context(resistance=resistance)
    ctx=replace(ctx,local_regime=NS(regime=NS(value=regime),direction=direction,parent_direction=direction))
    rows=[TradeTick(82000+i*1000,100,1,"Sell") for i in range(10)]
    rows += [TradeTick(96000+i*500,100+i*.01,30,"Buy") for i in range(8)]
    session=NS(symbol="AAAUSDT",market_context=ctx,trades=rows,
        candles=[NS(turnover=120000,confirmed=True)]*20)
    engine=NS(config=settings(tmp_path),clock=NS(time=lambda:100,perf_counter_ns=lambda:10**11))
    return engine,session


def test_countertrend_breakout_veto_and_local_hourly_override_preserved(tmp_path):
    from scalp_bot.trading_policy import prepare
    e,s=policy_fixture(tmp_path,direction=Trend.DOWN,regime="bearish_impulse")
    d=decision()
    assert prepare(e,s,d)=="countertrend_breakout_forbidden"
    e,s=policy_fixture(tmp_path)
    s.market_context=replace(s.market_context,htf_bias=NS(alignment="1h_only",trend_1h=Trend.DOWN))
    d.details["entryContextAssessment"]={"allowed":True,"htfOverride":"1h_only"}
    assert prepare(e,s,d) is None
    assert d.details["entryContextAssessment"]["htfOverride"]=="1h_only"


def test_real_obstacle_is_admission_blocker_not_size_discount(tmp_path):
    from test_semantic_arbiter import mature_level
    from scalp_bot.trading_policy import prepare
    level=mature_level("resistance",100.1,100.2,generation="other")
    e,s=policy_fixture(tmp_path,resistance=level)
    assert prepare(e,s,decision())=="strong_obstacle_before_first_take"


def test_countertrend_admission_requires_absorption_and_disables_runner(tmp_path):
    from scalp_bot.trading_policy import prepare
    e,s=policy_fixture(tmp_path,direction=Trend.DOWN,regime="bearish_trend")
    d=decision(strategy="weak_level_rejection")
    assert prepare(e,s,d)=="countertrend_requires_absorption_and_immediate_response"
    d.details.update(attackAbsorbed=True,microResponseReady=True)
    assert prepare(e,s,d) is None
    assert d.details["rejectionClass"]=="countertrend_reaction"
    assert d.details["allowRunner"] is False
    assert d.details["reactionBudget"]==.5
    assert d.details["noFollowThroughSeconds"]<e.config.weak_level_rejection_no_follow_through_seconds


def test_countertrend_position_exits_on_continued_opposing_bybit_flow():
    from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
    from scalp_bot.domain import Side
    context=NS(flow=NS(horizons={h:NS(trade_count=10,trade_imbalance=-.5) for h in (5,15,60)}))
    reason=WeakLevelRejectionStrategy().manage_position(side=Side.LONG,unrealized_pnl=1,
        opened_at=100,strategy_details={"rejectionClass":"countertrend_reaction"},decision=None,
        trend=Trend.DOWN,last_price=100,market_context=context,observed_at_ms=101000)
    assert reason=="countertrend_continuation_resumed"


def test_countertrend_no_follow_through_does_not_wait_for_large_loss():
    from scalp_bot.strategy.position_policy import should_exit_without_progress
    c=Settings(exchange_clock_enabled=True)
    pos=NS(opened_mono=0,opened_at=0,strategy="weak_level_rejection",initial_risk_usd=5,mfe_r=.1,
        strategy_details=dict(rejectionClass="countertrend_reaction",noFollowThroughSeconds=20))
    clock=NS(perf_counter_ns=lambda:20*10**9,time=lambda:20)
    assert should_exit_without_progress(c,clock,pos,.1)


def test_sweep_extreme_widens_real_strategy_stop():
    from test_strategies import weak_support_rejection_candles, rejection_absorption_only_flow
    from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
    # Same causal flow/zone as the production strategy regression, stronger low.
    rows=weak_support_rejection_candles()
    strategy=WeakLevelRejectionStrategy();strategy.staged_entries_enabled=True;strategy.sweep_stop_enabled=True
    book=OrderBook(bids=[(100.02,100)],asks=[(100.03,100)])
    d=strategy.evaluate(rows,book,Trend.UP,symbol="AAA",trades=rejection_absorption_only_flow())
    assert d.tradeable
    assert d.stop<d.details["sweepExtreme"]
    assert d.details["stopAnchorSource"]=="sweep_extreme"


def test_reachable_plan_can_pass_and_wider_stop_reduces_quantity(tmp_path):
    cfg=settings(tmp_path)
    book=OrderBook(bids=[(99.99,10000)],asks=[(100.,10000)])
    d=decision();engine=RiskEngine(cfg)
    before=engine.build_plan("XUSDT",d,1000,book,10000,20)
    assert before.allowed and before.plan.target==101
    assert before.plan.net_reward_risk>=1.15
    d.stop=99.6
    after=engine.build_plan("XUSDT",d,1000,book,10000,20)
    assert after.allowed
    assert after.plan.quantity<before.plan.quantity
    assert after.plan.expected_net_loss<=12.5


@pytest.mark.asyncio
async def test_demo_private_gap_and_clock_pause_resume_without_session_stop(tmp_path):
    import asyncio
    from scalp_bot.trading24h import DemoSession
    s=DemoSession(settings(tmp_path/"run"/"sessions"),Credentials("fake","fake","123"),tmp_path/"run")
    try:
        assert s.worker is None and s.adapter is None and s.engine.prepared_ranker is None
        async def reconcile(**kw):pass
        s.reconcile=reconcile
        s.engine._clock_state=lambda *a:dict(valid=True)
        await s.gap("reconnected");s.refresh_admission()
        assert s.portfolio.accepting
        await s.gap("private_ws_gap");s.refresh_admission()
        assert not s.portfolio.accepting and s.portfolio.stop_reason is None
        await s.gap("reconnected");s.refresh_admission()
        assert s.portfolio.accepting
        s.engine._clock_state=lambda *a:dict(valid=False)
        s.refresh_admission();s.engine._cancel_all_pending("clock_invalid")
        assert not s.portfolio.accepting and s.portfolio.stop_reason is None
        s.engine._clock_state=lambda *a:dict(valid=True)
        s.refresh_admission();assert s.portfolio.accepting
    finally:
        await s.engine.close();await s.rest.close()


@pytest.mark.asyncio
async def test_demo_public_gap_retains_owned_position_and_rejects_stale_book(tmp_path):
    from scalp_bot.trading24h import DemoSession
    from scalp_bot.engine import ActiveSymbolSession
    from scalp_bot.bybit import OrderBookState
    s=DemoSession(settings(tmp_path/"run"/"sessions"),Credentials("fake","fake","123"),tmp_path/"run")
    market=ActiveSymbolSession("AAA",book_synced=True,deep_book_synced=True)
    s.engine.sessions["AAA"]=market
    s.portfolio.by_symbol["AAA"]="owned"
    s.portfolio.books["AAA"]=OrderBook(bids=[(100,1)],asks=[(101,1)])
    fast,deep=OrderBookState(50),OrderBookState(1000)
    try:
        s.engine._invalidate_transport("AAA",dict(phase="fault",topics=["orderbook.50.AAA"]),fast,deep)
        assert s.portfolio.by_symbol["AAA"]=="owned"
        assert "AAA" not in s.portfolio.books and "owned" in s.portfolio.cancel_entries
        assert s.portfolio.stop_reason is None and not market.book_synced
    finally:
        s.portfolio.by_symbol.clear()
        await s.engine.close();await s.rest.close()


@pytest.mark.asyncio
async def test_empty_startup_universe_rescans_without_stopping(tmp_path):
    import asyncio
    from scalp_bot.engine import TradingEngine
    c=settings(tmp_path);c.exchange_clock_enabled=False;c.empty_startup_rescan_seconds=.1
    calls=[]
    class Rest:
        async def active_candidates(self):calls.append(1);return []
        async def close(self):pass
    e=TradingEngine(c,rest_client=Rest(),configure_observability=False)
    e.running=True
    try:
        await e.start();await asyncio.sleep(.25)
        assert len(calls)>=2 and e.running and not e._stop.is_set()
        assert e.market_health()["activeSymbolCount"]==0
    finally:await e.close()


@pytest.mark.asyncio
async def test_known_not_sent_entry_does_not_become_unknown_order(monkeypatch):
    from scalp_bot.demo_paper.transport import DemoRest, WriteNotSent
    rest=DemoRest(Credentials("x","y","z"),enabled=True);rest.write_enabled=True
    def reject(*args):raise SafetyError("entry source invalid before send")
    rest.before_write=reject
    try:
        with pytest.raises(WriteNotSent):await rest.request("POST","/v5/order/create",{})
    finally:await rest.close()


@pytest.mark.asyncio
async def test_skipped_dispatch_releases_reservation_after_temporary_pause():
    import asyncio
    from demo_paper.test_contracts import make_pair, plan
    from scalp_bot.demo_paper.contracts import Intent
    portfolio,arms,spec,events=make_pair()
    intent=Intent.freeze("r",plan(),1,1)
    assert portfolio.admit(intent,spec)[0]
    portfolio.accepting=False
    tasks=[asyncio.create_task(portfolio.dispatch(name)) for name in arms]
    try:
        await asyncio.sleep(.01)
        portfolio.release_reconciled()
        assert not portfolio.reservations and not portfolio.by_symbol and portfolio.stop_reason is None
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_stop", [False, True])
async def test_rule_only_demo_full_lifecycle_and_graceful_finalization(tmp_path,monkeypatch,duration_stop):
    import asyncio
    import time
    import scalp_bot.trading24h as runtime
    from demo_paper.test_session import Exchange
    from demo_paper.test_contracts import plan
    from scalp_bot.demo_paper.contracts import Intent
    from scalp_bot.engine import ActiveSymbolSession
    from scalp_bot.instrument import InstrumentSpec
    root=tmp_path/"run";cfg=settings(root/"sessions");cfg.exchange_clock_enabled=False
    s=runtime.DemoSession(cfg,Credentials("FAKE","SECRET","123"),root)
    exchange=Exchange()
    async def initialize():
        await s.rest.close();s.rest=exchange;s.arms["demo"].venue.rest=exchange
    s.initialize_rest=initialize
    async def preflight(*args):return dict(wallet=dict(list=[dict(coin=[dict(coin="USDT",equity="1000")])]))
    async def private(credentials,on_message,on_gap,stop,**kwargs):
        await on_gap("reconnected");await stop.wait()
    monkeypatch.setattr(runtime,"connected_preflight",preflight)
    monkeypatch.setattr(runtime,"private_stream",private)
    elapsed=[False]
    monkeypatch.setattr(runtime,"duration_elapsed",lambda *a:elapsed[0])
    spec=InstrumentSpec("BTCUSDT","Trading",.01,.001,.001,5,100,100,480,100)
    book=OrderBook(bids=[(99.99,100)],asks=[(100,100)])
    async def bootstrap():
        market=ActiveSymbolSession("BTCUSDT",clock=s.clock)
        market.orderbook=market.deep_orderbook=book
        market.last_book_at=market.last_deep_book_at=time.time()
        market.book_synced=market.deep_book_synced=True
        market.instrument=spec
        s.engine.sessions["BTCUSDT"]=market
        s.portfolio.books["BTCUSDT"]=book;s.arms["paper"].venue.market("BTCUSDT",book)
        async def enter_stop():
            while not s.portfolio.accepting:await asyncio.sleep(.001)
            assert s.portfolio.admit(Intent.freeze(s.run,plan(),1,time.perf_counter_ns()),spec)[0]
            until=time.monotonic()+3
            while time.monotonic()<until and not all(a.remaining("BTCUSDT")==1 for a in s.arms.values()):await asyncio.sleep(.005)
            assert all(a.remaining("BTCUSDT")==1 for a in s.arms.values())
            # A private disconnect pauses new entries without surrendering protection.
            await s.gap("private_ws_gap")
            assert s.portfolio.stop_reason is None
            await s.gap("reconnected")
            if duration_stop:elapsed[0]=True
            else:s.stop.set()
        s.tasks.append(asyncio.create_task(enter_stop()))
    s.engine.start=bootstrap
    result=await asyncio.wait_for(s.run_session(),10)
    assert result["finalized"] and exchange.position==0
    assert result["reason"]==("duration_elapsed" if duration_stop else "operator_stop")
    assert s.worker is None and s.adapter is None
    creates=[p for m,path,p in exchange.calls if path.endswith("create")]
    assert sum(not p["reduceOnly"] for p in creates)==1
    assert all(p["reduceOnly"] for p in creates[1:])
    assert (root/"summary.json").is_file()
