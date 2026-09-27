from copy import deepcopy
from dataclasses import replace
from time import time

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.engine import TradingEngine, ActiveSymbolSession
from scalp_bot.parallel_scenarios import ParallelScenarioRouter, PRIORITY
from scalp_bot.scenario import Scenario
from test_scenario_remediation import market, level, ready, SYMBOL


def seed(engine, session, owner, *, side="long", first=1):
    now = engine.clock.perf_counter_ns()/1e9
    s = Scenario(session.symbol, owner+":test", owner, side, owner+":g", 100, 1,
                 now, now, now+300, [], first_signal_mono=first)
    engine.router.children[owner].scenarios[session.symbol] = s
    if session.symbol not in engine.router.scenarios:
        engine.router.scenarios[session.symbol] = s
    return s


async def make_engine(tmp_path):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path), exchange_clock_enabled=False,
        trend_structure_enabled=True, price_action_hypothesis_enabled=True,
        min_net_profit_usd=0,min_net_profit_equity_fraction=0))
    session = ActiveSymbolSession("AAA",orderbook=OrderBook([(100,10000)],[(100.01,10000)]),
        last_price=100,last_market_at=time(),last_book_at=time(),book_synced=True)
    engine.sessions["AAA"] = session
    engine.running = True
    return engine, session


@pytest.mark.parametrize("opposite",[False,True])
async def test_ready_selection_one_order_one_owner_and_no_queued_reentry(tmp_path, opposite):
    engine, session = await make_engine(tmp_path)
    try:
        for i,owner in enumerate(("weak_level_rejection","level_breakout")):
            side = "short" if opposite and i else "long"
            seed(engine,session,owner,side=side,first=1)
            sign = 1 if side == "long" else -1
            session.decisions[owner] = StrategyDecision(owner,Action(side),[],entry=100.01,
                stop=100.01-sign*.5,target=100.01+sign*2,setup_id=owner)
        engine._arbitrate_once()
        assert len(engine.broker.positions)==1 and not engine.broker.pending_entries
        assert engine.broker.positions["AAA"].strategy=="level_breakout"  # stable tie-break
        assert engine.router.scenario_for("AAA","weak_level_rejection").last_rejection["reason"] == "earlier_eligible_ready_proposal"
        engine._arbitrate_once()
        assert len(engine.broker.positions)==1
        engine.router.completed("AAA",engine.clock.perf_counter_ns()/1e9,"stop")
        assert engine.router.scenario_for("AAA","weak_level_rejection").state=="INVALIDATED"
        assert not engine._scenario_entry_valid(session,session.decisions["weak_level_rejection"])
    finally:
        await engine.close()


@pytest.mark.parametrize("waiter", PRIORITY)
async def test_each_waiting_owner_cannot_block_independent_ready(tmp_path, waiter):
    engine, session = await make_engine(tmp_path)
    try:
        chosen = next(owner for owner in PRIORITY if owner != waiter)
        seed(engine,session,waiter)
        seed(engine,session,chosen,first=2)
        session.decisions[waiter]=StrategyDecision(waiter,Action.WAIT,["independent preparation"])
        session.decisions[chosen]=StrategyDecision(chosen,Action.LONG,[],entry=100.01,stop=99.5,target=102,setup_id=chosen)
        engine._arbitrate_once()
        assert engine.broker.positions["AAA"].strategy==chosen
    finally:
        await engine.close()


async def test_risk_refusal_is_local_and_reserving_is_busy_without_position(tmp_path):
    engine,session=await make_engine(tmp_path)
    try:
        for owner in PRIORITY[:2]:
            seed(engine,session,owner)
            session.decisions[owner]=StrategyDecision(owner,Action.LONG,[],entry=100.01,stop=99.5,
                target=100.02 if owner=="level_breakout" else 102,setup_id=owner)
        engine._arbitrate_once()
        assert engine.broker.positions["AAA"].strategy=="weak_level_rejection"
        assert engine.router.scenario_for("AAA","level_breakout").last_rejection["owner"]=="risk"
        engine.broker.positions.clear()  # Model unknown/in-flight execution, not a free symbol.
        assert not engine._scenario_entry_valid(session,session.decisions["level_breakout"])
    finally:
        await engine.close()


def test_shared_assessment_and_context_isolation_and_local_cancellation():
    a,b=level(),level("support","B",mature=False,center=100)
    rows,book,context,structure=market([a,b])
    router=ParallelScenarioRouter()
    router.observe(SYMBOL,context,rows,structure,{k:True for k in PRIORITY},10)
    first=router.scenario_for(SYMBOL,"level_breakout")
    second=router.scenario_for(SYMBOL,"weak_level_rejection")
    assert first and second and first.scenario_id!=second.scenario_id
    own=router.context_for(context,SYMBOL,second.owner)
    assert context.scenario is None and own.scenario["owner"]==second.owner
    own.scenario["reasons"].append("mutation")
    assert "mutation" not in second.reasons
    router.transition(first,"INVALIDATED",11,"object invalid")
    assert second.state=="ASSIGNED"
    assert router.public(SYMBOL)["schemaVersion"]==3


async def test_actual_evaluation_isolates_each_context_and_strategy_exception(tmp_path):
    from copy import deepcopy
    from scalp_bot.runtime_clock import ReplayRuntimeClock
    a,b=level(),level("support","B",mature=False,center=100)
    rows,book,context,structure=market([a,b])
    engine=TradingEngine(Settings(_env_file=None,session_dir=str(tmp_path),exchange_clock_enabled=False,
        trend_structure_enabled=True,price_action_hypothesis_enabled=True,density_enabled=False),
        clock=ReplayRuntimeClock(wall_seconds=context.observed_at_ms/1000,mono_ns=10**10))
    session=ActiveSymbolSession(SYMBOL,candles=rows,orderbook=book,structure=structure,
        last_price=book.mid,last_book_at=context.observed_at_ms/1000,book_synced=True,clock=engine.clock)
    engine.sessions[SYMBOL]=session
    seen=[]
    def assess(context,candles,structure,enabled,**kwargs):
        return {"status":"OBSERVING","rangeAbs":1},[dict(owner=owner,side="long",anchor=100,
            signature=owner+":g",priority=1,distance=0,object_ref=None,episode_key=owner+":episode",reasons=[])
            for owner in PRIORITY]
    engine.router.assess=assess
    for owner in PRIORITY:
        def evaluate(*args,_owner=owner,**kwargs):
            seen.append((_owner,deepcopy(kwargs["market_context"].scenario)))
            if _owner=="level_breakout":raise ValueError("isolated strategy error")
            return StrategyDecision(_owner,Action.WAIT,["waiting independently"])
        engine.strategies[owner].evaluate=evaluate
    try:
        await engine._evaluate(session)
        assert len(seen)==len(PRIORITY) and {owner for owner,_ in seen}==set(PRIORITY)
        assert all(owner==scenario["owner"] for owner,scenario in seen)
        assert session.market_context.scenario is None
        assert session.decisions["level_breakout"].details["state"]=="error"
        assert all(owner in session.decisions for owner in PRIORITY)
    finally:await engine.close()
