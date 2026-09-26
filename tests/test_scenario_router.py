"""Single-owner lifecycle and causal contracts; no network or profitability claims."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scalp_bot.domain import Action, Candle, OrderBook, Side, StrategyDecision, Trend
from scalp_bot.execution_book import coherent_execution_book, execution_quality
from scalp_bot.scenario import ROLES, ScenarioRouter
from scalp_bot.strategy.base import Strategy
from scalp_bot.strategy.market_context import build_structure_context
from scalp_bot.strategy.regime import LocalRegime
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel, TrendLine
from scalp_bot.strategy.targets import structural_target
from test_price_action_hypothesis import scenario as beta_market


def market(kind="breakout", direction=1):
    rows, book, context, _ = beta_market(direction)
    rows = [replace(rows[0],start_ms=rows[0].start_ms-(25-i)*60_000) for i in range(25)]+rows
    level=StructuralLevel("resistance",100.19,100.21,3,"1m",1,generation_id="level:g1")
    if kind == "breakout":
        level.touches = 6
        level.distinct_approaches = 5
        level.reaction_pct = .003
        level.lifecycle = "worked"
    elif kind == "rejection":
        level.distinct_approaches = 1
    structure=MarketStructure([level])
    if direction<0:
        level.kind="support";level.low=99.79;level.high=99.81
    if kind=="rejection":
        level.kind="support" if direction>0 else "resistance"
        level.low,level.high=(100.09,100.11) if direction>0 else (99.89,99.91)
    if kind=="trend":
        structure.levels=[]
        structure.trendlines=[TrendLine("support" if direction>0 else "resistance",
            "1m",1,2,100,100,context.last_price,3,1,.01*direction)]
        context=replace(context,local_regime=replace(context.local_regime,regime=LocalRegime.PULLBACK))
    if kind=="beta":
        structure=MarketStructure()
    context=replace(context,structure=build_structure_context(structure,context.last_price))
    return rows,book,context,structure


def assign(kind="breakout",direction=1):
    r=ScenarioRouter();rows,book,context,structure=market(kind,direction)
    enabled={k:True for k in ROLES}
    s=r.observe("TESTUSDT",context,rows,structure,enabled,10)
    assert s is not None
    return r,s,rows,book,context,structure,enabled


@pytest.mark.parametrize("kind,owner",[("breakout","level_breakout"),("rejection","weak_level_rejection"),
    ("trend","trend_structure"),("beta","price_action_hypothesis")])
@pytest.mark.parametrize("direction",[1,-1])
def test_each_supported_module_is_selected_before_a_signal(kind,owner,direction):
    r,s,*_=assign(kind,direction)
    assert s.owner==owner
    assert s.side==("long" if direction>0 else "short")
    assert s.first_signal_mono is None
    assert r.public(s.symbol)["situation"]["strategies"]["orderbook_density"]["status"]=="evidence_only"


def test_disabled_insufficient_and_no_scenario_are_distinct():
    rows,b,c,structure=market("beta")
    enabled={k:False for k in ROLES};enabled['orderbook_density']=True
    r=ScenarioRouter()
    assert r.observe('X',c,rows,structure,enabled,0) is None
    assert r.public('X')['situation']['status']=='NO_SUITABLE_SCENARIO'
    assert r.public('X')['situation']['strategies']['price_action_hypothesis']['status']=='disabled'
    r.observe('Y',c,rows[:3],structure,enabled,0)
    assert r.public('Y')['situation']['status']=='INSUFFICIENT_DATA'
    # A beta does not become a universal fallback without the named pattern.
    plain=[replace(x,open=100,close=100,volume=100) for x in rows]
    assert ScenarioRouter().observe('Z',c,plain,structure,{k:True for k in ROLES},0) is None


def test_small_changes_do_not_switch_owner_but_real_departure_invalidates():
    r,s,rows,b,c,st,en=assign()
    # This test isolates rearming of the SAME level. Independent candle/level
    # scenarios are intentionally not consumed by completion (R04).
    en = {s.owner: True}
    changed=replace(c,last_price=c.last_price+.001)
    assert r.observe(s.symbol,changed,rows,st,en,11) is s
    far=replace(c,last_price=c.last_price+1)
    assert r.observe(s.symbol,far,rows,st,en,12) is None
    assert s.state=='INVALIDATED'
    assert r.observe(s.symbol,c,rows,st,en,13) is None  # no risk-retry reassignment
    r.observe(s.symbol,far,rows,st,en,14)  # observed departure rearms an approach
    new=r.observe(s.symbol,c,rows,st,en,15)
    assert new and new.scenario_id!=s.scenario_id


def decision(s,book):
    sign=1 if s.side=='long' else -1
    entry=book.executable_entry(Side(s.side))
    return StrategyDecision(s.owner,Action(s.side),[],entry=entry,stop=entry-sign*.3,
        target=entry+sign*.8,setup_id='causal:g1',details={'state':'impulse',
        'opportunityArm':{'price':s.anchor,'observedAtMs':1000},
        'fireTrigger':{'price':entry,'observedAtMs':2000}, 'levelLifecycle':deepcopy(s.level)})


@pytest.mark.parametrize('direction',[1,-1])
def test_risk_rejection_target_change_cannot_reset_entry_age_or_budget(direction):
    r,s,rows,b,c,st,en=assign(direction=direction)
    first=r.accept_decision(s.symbol,decision(s,b),12,b)
    original=deepcopy(first.details['opportunityTrigger']); expiry=s.expires_mono
    r.reject(s.symbol,'risk','net reward too small',13)
    repeat=decision(s,b);repeat.target+=direction*10;repeat.setup_id='renamed:g2'
    repeat.details['fireTrigger']['observedAtMs']=50000
    last=r.accept_decision(s.symbol,repeat,20,b)
    assert last.target==first.target and last.setup_id==first.setup_id
    assert last.details['opportunityTrigger']==original
    assert s.first_signal_mono==12 and s.expires_mono==expiry
    chased=OrderBook([(b.best_bid+direction*.2,1000)],[(b.best_ask+direction*.2,1000)])
    assert not r.accept_decision(s.symbol,decision(s,chased),21,chased).tradeable
    assert s.state=='INVALIDATED'


def test_cancel_ack_fill_race_and_settings_keep_execution_owner():
    r,s,rows,b,c,st,en=assign()
    d=r.accept_decision(s.symbol,decision(s,b),11,b)
    r.submitted(s.symbol,12)
    pending=SimpleNamespace(plan=SimpleNamespace(strategy=s.owner,side=Side(s.side)))
    en[s.owner]=False
    assert r.observe(s.symbol,c,rows,st,en,13,pending=pending) is s
    assert s.state=='ORDER_PENDING' and s.cancellation=='strategy_disabled'
    # A fill delivered before the cancellation acknowledgement wins ownership.
    r.filled(s.symbol,14)
    r.cancelled(s.symbol,15,'late cancel ack')
    pos=SimpleNamespace(strategy=s.owner,side=Side(s.side))
    assert r.observe(s.symbol,c,rows,st,en,16,position=pos) is s
    assert s.state=='IN_POSITION'
    r.completed(s.symbol,17,'stop')
    assert s.state=='COMPLETED'


@pytest.mark.parametrize('owner',['level_breakout','weak_level_rejection'])
@pytest.mark.parametrize('side',[Side.LONG,Side.SHORT])
def test_entry_cannot_allow_already_invalid_exit_side(owner,side):
    strategy=Strategy();strategy.key=owner
    d=StrategyDecision(owner,Action(side.value),[],entry=100,stop=99 if side==Side.LONG else 101,
        target=102 if side==Side.LONG else 98,details={'zone':{'low':99.99,'high':100.01}})
    invalid=OrderBook([(99.98,100)],[(100.02,100)])
    assert strategy.entry_invalidation(d,invalid)
    good=OrderBook([(100.02,100)],[(100.03,100)]) if side==Side.LONG else OrderBook([(99.97,100)],[(99.98,100)])
    assert strategy.entry_invalidation(d,good) is None


@pytest.mark.parametrize('side',[Side.LONG,Side.SHORT])
def test_fast_head_replaces_stale_depth_without_duplicate_quantity(side):
    fast=OrderBook([(100,2),(99,3)],[(101,2),(102,3)])
    deep=OrderBook([(100.5,10),(100,900),(99,800),(98,7)],[(100.6,10),(101,900),(102,800),(103,7)])
    book=coherent_execution_book(fast,deep)
    assert book.bids==[(100,2),(99,3),(98,7)]
    assert book.asks==[(101,2),(102,3),(103,7)]
    assert book.execution['deepQuoteConflict']
    price,filled,_=book.exit_vwap_quantity(side,6)
    assert filled==6
    assert price==pytest.approx((100*2+99*3+98)/6 if side==Side.LONG else (101*2+102*3+103)/6)
    assert execution_quality(book,12,20)['quality']=='estimated_missing_depth'


def test_targets_skip_wrong_side_but_not_close_obstacles_for_reward():
    levels=[SimpleNamespace(price=p) for p in (99,100.2,104)]
    assert structural_target(100,Action.LONG,levels,movement=1)[0]==100.2
    assert structural_target(100,Action.LONG,[],movement=.4)[0]==100.4
    assert structural_target(100,Action.LONG,[],movement=None)[2]=='insufficient_movement_data'


def test_late_fill_after_ack_restores_original_generation_and_invalidates_new_owner():
    r,s,rows,b,c,st,en=assign()
    r.accept_decision(s.symbol,decision(s,b),11,b)
    original = s.public()
    r.submitted(s.symbol,12)
    r.cancelled(s.symbol,13,'cancel acknowledged')
    # Model a different, genuinely new assignment before a delayed fill notice.
    replacement=deepcopy(s)
    replacement.scenario_id='new-generation'
    replacement.owner='weak_level_rejection'
    replacement.state='ASSIGNED'
    r.scenarios[s.symbol]=replacement
    position=SimpleNamespace(strategy=s.owner,side=Side(s.side),entry=b.best_ask,
        setup_id=s.setup_id,strategy_details={'scenario':original})
    restored=r.restore_execution(s.symbol,position,14)
    r.filled(s.symbol,14)
    r.cancelled(s.symbol,15,'duplicate cancel acknowledgement')
    assert restored.scenario_id==s.scenario_id and restored.owner==s.owner
    assert restored.state=='IN_POSITION'
    assert replacement.state=='INVALIDATED'


def test_real_failed_break_allows_opposite_scenario_on_next_observation():
    # Session extremes can legitimately support both playbooks. An ordinary
    # worked horizontal level is not a young weak-level rejection candidate.
    rows,b,c,st=market()
    st.levels[0].kind="day_high"
    en={k:True for k in ROLES}; r=ScenarioRouter()
    s=r.observe("TESTUSDT",c,rows,st,en,10)
    assert s and s.owner=="level_breakout"
    # Existing resistance was pierced, then the observed price reclaimed below.
    changed = replace(c, last_price=100.17)
    rows[-1]=replace(rows[-1],high=100.3,low=100.15,close=100.17)
    assert r.observe(s.symbol,changed,rows,st,en,11) is None
    assert s.state=='INVALIDATED'
    fresh=r.observe(s.symbol,changed,rows,st,en,12)
    assert fresh and fresh.owner=='weak_level_rejection' and fresh.side=='short'


def test_old_cancel_ack_cannot_cancel_new_order_even_with_reused_setup_name():
    r,s,*_=assign()
    s.setup_id='same-level-name'
    s.state='ORDER_PENDING'
    r.cancelled(s.symbol,12,'late cancellation',setup_id=s.setup_id,scenario_id='old-generation')
    assert s.state=='ORDER_PENDING'
    r.cancelled(s.symbol,13,'current cancellation',setup_id=s.setup_id,scenario_id=s.scenario_id)
    assert s.state=='INVALIDATED'


def test_enabled_module_with_short_history_reports_insufficient_not_unsuitable():
    rows,b,c,st=market()
    r=ScenarioRouter()
    assert r.observe('X',c,rows[-25:],st,{'level_breakout':True},0) is None
    assert r.public('X')['situation']['status']=='INSUFFICIENT_DATA'
