"""Full lifecycle economics must choose an executable exit plan, not widen targets."""
from copy import deepcopy
from dataclasses import fields
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.risk import RiskEngine
from scalp_bot.trading24h import settings


def setup(direction=1, distance=.70):
    entry = 100.
    return StrategyDecision('trend_structure', Action.LONG if direction>0 else Action.SHORT, [],
        entry=entry, stop=entry-direction*.3, target=entry+direction*distance,
        details={'marketTargetOnly': True, 'allowRunner': True,
            'opportunityTrigger': {'price': entry, 'observedAtMs': 1000, 'expectedImpulsePct': .01}})


@pytest.mark.parametrize('direction', [1, -1])
def test_single_reachable_target_when_partial_would_reject_entire_plan(tmp_path, direction):
    config = settings(tmp_path)
    book = OrderBook([(99.99,10000)],[(100.,10000)])
    d=setup(direction)
    full_config=config.model_copy(update={'partial_take_enabled': False})
    full=RiskEngine(full_config).build_plan('X',deepcopy(d),1000,book,10000,20)
    assert full.allowed
    result=RiskEngine(config).build_plan('X',d,1000,book,10000,20)
    assert result.allowed, result.reason
    plan=result.plan
    assert plan.strategy_details['economics']['partialPlanned'] is False
    assert plan.target==full.plan.target and plan.stop==full.plan.stop
    assert plan.quantity==full.plan.quantity
    assert plan.net_reward_risk==pytest.approx(full.plan.net_reward_risk)
    assert plan.target==d.target
    assert plan.strategy_details['economics']['exitPlanSelection']['selected']=='single_reachable_target'


def test_preserves_partial_when_complete_lifecycle_is_qualified(tmp_path):
    d=setup(distance=1.)
    result=RiskEngine(settings(tmp_path)).build_plan('X',d,1000,OrderBook([(99.99,10000)],[(100.,10000)]),10000,10000)
    assert result.allowed
    assert result.plan.strategy_details['economics']['partialPlanned'] is True


def test_rejects_when_both_exit_plans_fail_existing_policy(tmp_path):
    result=RiskEngine(settings(tmp_path)).build_plan('X',setup(distance=.4),1000,
        OrderBook([(99.99,10000)],[(100.,10000)]),10000,20)
    assert not result.allowed
    assert result.diagnostics['absoluteMinimumNetRewardRisk']==1.
    assert result.diagnostics['requiredNetRewardRisk']==1.15


def test_countertrend_reaction_never_gains_partial_or_runner(tmp_path):
    d=setup(distance=.7);d.strategy='weak_level_rejection'
    d.details.update(allowRunner=False,rejectionClass='countertrend_reaction',reactionBudget=.7)
    result=RiskEngine(settings(tmp_path)).build_plan('X',d,1000,OrderBook([(99.99,10000)],[(100.,10000)]),10000,20)
    assert result.allowed
    assert not result.plan.strategy_details['economics']['partialPlanned']
    assert result.plan.target<=100.7


def test_original_profile_partial_behavior_is_unchanged(tmp_path):
    config=settings(tmp_path);config.trading_quality_enabled=False
    result=RiskEngine(config).build_plan('X',setup(),1000,OrderBook([(99.99,10000)],[(100.,10000)]),10000,20)
    assert not result.allowed
    assert result.diagnostics['partialPlanned']


def test_recorded_pumpfun_plan_with_market_target_and_real_costs(tmp_path):
    from scalp_bot.domain import Candle
    from scalp_bot.instrument import InstrumentSpec
    from scalp_bot.strategy.liquidity import find_liquidity_targets
    from scalp_bot.strategy.structure import market_structure_from_public
    from scalp_bot.strategy.targets import structural_target, movement_budget
    f=json.loads((Path(__file__).parent/'fixtures/trading/partial_exit_pumpfun.json').read_text())
    d=StrategyDecision(**{**f['decision'], 'action': Action(f['decision']['action'])})
    rows=[Candle(int(c['time']*1000), **{k:v for k,v in c.items() if k!='time'}) for c in f['candles']]
    assert all(c.confirmed and c.start_ms+60000<=f['observedAt']*1000 for c in rows)
    ladder=find_liquidity_targets(rows,d.entry,d.action,min_distance_pct=0,
        structure=market_structure_from_public(f['structure']),unconsumed_swings_only=True)
    d.target,_,_=structural_target(d.entry,d.action,ladder,movement=movement_budget(rows))
    assert d.target==.005226  # nearest unconsumed low, independent of requested RR
    d.details['liquidityLadder']=[x.public() for x in ladder]
    def book(name):
        return OrderBook(**{side:[tuple(x[:2]) for x in f[name][side]] for side in ('bids','asks')})
    spec=InstrumentSpec(**{field.name:f['instrument'][field.name] for field in fields(InstrumentSpec)})
    config=settings(tmp_path)
    result=RiskEngine(config).build_plan('PUMPFUNUSDT',d,1000,book('fastBook'),10000,20,
        depth_book=book('deepBook'),instrument=spec)
    assert result.allowed, result.reason
    plan=result.plan;econ=plan.strategy_details['economics'];choice=econ['exitPlanSelection']
    assert choice['partial']['netRewardRisk']<1.15<=plan.net_reward_risk
    assert choice['selected']=='single_reachable_target' and not econ['partialPlanned']
    assert plan.target==.005227  # one tick before resting liquidity; reward was reduced
    assert plan.expected_net_loss<=12.5 and plan.notional<=5000
    assert econ['winnerCostShare']<=.35


@pytest.mark.parametrize('direction',[1,-1])
def test_broker_obeys_selected_full_exit_and_keeps_stop(tmp_path,direction):
    from scalp_bot.paper import PaperBroker
    config=settings(tmp_path)
    b=OrderBook([(99.99,10000)],[(100.,10000)])
    plan=RiskEngine(config).build_plan('X',setup(direction),1000,b,10000,20).plan
    broker=PaperBroker(config);pos=broker.open(plan,b)
    original_stop=pos.stop
    midpoint=pos.entry+direction*abs(pos.entry-pos.stop)*1.1
    assert broker.mark('X',midpoint,OrderBook([(midpoint-.001,10000)],[(midpoint+.001,10000)]))==[]
    assert not pos.partial_taken and pos.stop==original_stop
    crossed=plan.target+direction*.01
    events=broker.mark('X',crossed,OrderBook([(crossed-.001,10000)],[(crossed+.001,10000)]))
    assert events[-1]['reason']=='target'
    assert not broker.positions
    assert events[-1]['netPnl']==pytest.approx(plan.expected_net_profit)


@pytest.mark.parametrize('direction',[1,-1])
def test_demo_management_requests_full_target_and_retains_hard_stop(tmp_path,direction):
    from scalp_bot.demo_paper.portfolio import Arm
    from scalp_bot.demo_paper.contracts import Intent
    from scalp_bot.runtime_clock import SystemRuntimeClock
    config=settings(tmp_path);b=OrderBook([(99.99,10000)],[(100.,10000)])
    plan=RiskEngine(config).build_plan('XUSDT',setup(direction),1000,b,10000,20).plan
    clock=SystemRuntimeClock();arm=Arm('demo',config,clock,lambda *a:None)
    arm.broker.open(plan,b)  # fixture position; no network/orders
    intent=Intent.freeze('exit-policy-test',plan,clock.perf_counter_ns(),1)
    orders=arm.desired(intent,b,now_ns=clock.perf_counter_ns())
    assert len(orders)==1
    assert orders[0][0]=='target' and orders[0][2]=='PostOnly'
    assert float(orders[0][1])==plan.quantity and orders[0][3]==plan.target
    through_stop=plan.stop-direction*.01
    stopped=arm.desired(intent,OrderBook([(through_stop-.001,10000)],[(through_stop+.001,10000)]),
        now_ns=clock.perf_counter_ns())
    assert len(stopped)==1 and stopped[0][0]=='stop' and stopped[0][2]=='Market'
    assert float(stopped[0][1])==plan.quantity


@pytest.mark.parametrize('direction',[1,-1])
def test_first_exit_cannot_be_projected_beyond_reachable_target(tmp_path,direction):
    from dataclasses import replace
    from scalp_bot.trading_policy import prepare, breakout_path
    from test_trading24h import policy_fixture, decision
    from test_semantic_arbiter import mature_level
    from scalp_bot.domain import Trend
    level=mature_level('resistance' if direction>0 else 'support',
        100.6 if direction>0 else 99.3,100.7 if direction>0 else 99.4,generation='other')
    engine,session=policy_fixture(tmp_path,direction=Trend.UP if direction>0 else Trend.DOWN,
        regime='bullish_trend' if direction>0 else 'bearish_trend')
    from test_semantic_arbiter import context
    structure=context(resistance=level).structure if direction>0 else context(support=level).structure
    session.market_context=replace(session.market_context,structure=structure)
    session.instrument=SimpleNamespace(tick_size=.01)
    d=decision('long' if direction>0 else 'short')
    d.stop=99 if direction>0 else 101;d.target=100.6 if direction>0 else 99.4
    d.details.update(preparationOnly=True,liquidityLadder=[{'price':d.target}])
    assert prepare(engine,session,d) is None
    path=d.details['reachableStructuralPath']
    assert path['firstTakePrice']==pytest.approx(100.59 if direction>0 else 99.41)
    assert not path['obstacleBeforeFirstTake']
    # A current quote already inside the independent opposing zone is blocked.
    assert breakout_path(d,session.market_context,engine.config,executable=100.65 if direction>0 else 99.35).obstacle_before_first_take
