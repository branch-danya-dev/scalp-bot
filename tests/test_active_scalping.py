"""Activity policy and independent cash-flow checks; no fitted win-rate claim."""
from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from scalp_bot.domain import Action, OrderBook, Side
from scalp_bot.execution import FeeSchedule
from scalp_bot.risk import RiskEngine
from scalp_bot.trading24h import ACTIVE_PROFILE, settings, manifest
from scalp_bot.trading_policy import participation_quality
from test_trading_exit_policy import setup


def test_active_profile_changes_activity_without_increasing_risk(tmp_path):
    old = settings(tmp_path)
    c = settings(tmp_path, profile=ACTIVE_PROFILE)
    assert c.min_net_reward_risk == c.absolute_min_net_reward_risk == 1.
    assert c.max_winner_cost_share == .5
    assert c.working_symbols == c.max_active_symbols == 12
    assert c.liquid_universe_size == 60 and c.min_turnover_usd == 50_000_000
    for field in ('risk_fraction', 'max_trade_all_in_loss_fraction', 'max_total_risk_fraction',
                  'max_leverage', 'max_position_leverage', 'max_open_positions',
                  'max_pending_entries', 'book_stale_seconds', 'deep_book_stale_seconds',
                  'max_entry_drift_bps', 'taker_fee_rate', 'maker_fee_rate', 'slippage_bps'):
        assert getattr(c, field) == getattr(old, field)
    m = manifest(c)
    assert m['profile'] == ACTIVE_PROFILE and m['durationSeconds'] == 86400
    assert not any(m['mlAuthority'].values()) and not any(m['artificialLimits'].values())
    demo = settings(tmp_path, equity=50000, profile=ACTIVE_PROFILE)
    assert demo.start_balance == 50000
    assert demo.start_balance*demo.risk_fraction == 5
    assert demo.start_balance*demo.max_position_leverage == 5000


@pytest.mark.parametrize('notional,count_rate,pace,acceleration,allowed', [
    (10000, 1., 1., .8, True),  # Steady participation need not keep accelerating.
    (13000, 1.3, .336, 1.2, True),  # New burst during a formerly quiet minute.
    (1500, 1.3, .336, 2., False),  # XPL-like thin imbalance remains blocked.
    (13000, .2, .336, 2., False),  # One large trade is not broad participation.
    (8000, 1., .336, 1.2, False),
])
def test_active_participation_uses_current_notional_and_count(tmp_path, notional,count_rate,pace,acceleration,allowed):
    flow = dict(baselineReady=True,notional5s=notional,acceleration=acceleration,
        tradeRateRatio=count_rate,tradeCount5s=10,imbalance5s=.7,priceMove5sPct=.001)
    candles=[NS(confirmed=True,turnover=120000.)]*20
    cfg=settings(tmp_path, profile=ACTIVE_PROFILE)
    assert participation_quality(flow,candles,NS(volume_pace_ratio=pace),'long',cfg)['confirmed'] == allowed
    flow['priceMove5sPct']=-.001
    assert not participation_quality(flow,candles,NS(volume_pace_ratio=pace),'long',cfg)['confirmed']


@pytest.mark.parametrize('require_position,expected', [(True,Action.WAIT),(False,Action.LONG)])
def test_rejection_can_respond_before_crossing_midpoint_of_minute(require_position,expected):
    from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
    from scalp_bot.strategy.structure import StructuralLevel, MarketStructure
    from scalp_bot.domain import Trend, TradeTick
    from test_strategy_level_semantics import rejection_candles
    strategy=WeakLevelRejectionStrategy()
    strategy.staged_entries_enabled=False
    strategy.require_forming_position=require_position
    level=StructuralLevel('previous_day_low',100.,100.,1,'1D',.88,generation_id='pdl1')
    structure=MarketStructure(levels=[level])
    ticks=[TradeTick(20_000_000+i*200,100.,4,'Sell') for i in range(10)]
    ticks.append(TradeTick(20_002_200,99.95,2,'Sell'))
    forming=NS(close_position=.3,micro_move_5s_bps=0.,high=100.5,low=99.95,body_pct=.001,public=lambda:{})
    context=NS(local_regime=None,forming_candle=forming,scenario=None,
        flow_alignment_for=lambda _:None,liquidity_alignment_for=lambda _:None,execution=NS(ready=True))
    common=dict(symbol='RESPONSEUSDT',trades=ticks,structure=structure,market_context=context)
    armed=strategy.evaluate(rejection_candles(),OrderBook([(100.09,50)],[(100.1,50)]),
        Trend.UP,observed_at_ms=20_002_200,**common)
    assert armed.action==Action.WAIT
    fired=strategy.evaluate(rejection_candles(),OrderBook([(100.115,50)],[(100.125,50)]),
        Trend.UP,observed_at_ms=20_003_200,**common)
    assert fired.action==expected


def walk_quantity(levels, quantity):
    """Independent Decimal cash ledger; no OrderBook/RiskEngine cost helpers."""
    remaining=quantity; cash=D(0)
    for price,qty in levels:
        take=min(remaining,D(str(qty)))
        cash+=take*D(str(price));remaining-=take
        if remaining==0: break
    assert remaining==0
    return cash/quantity


@pytest.mark.parametrize('direction',[1,-1])
@pytest.mark.parametrize('partial',[True,False])
@pytest.mark.parametrize('maker,taker',[(.0002,.00055),(.0001,.0003)])
@pytest.mark.parametrize('step',[None,5.])
def test_independent_decimal_cash_ledger_matches_risk_and_depth(tmp_path,direction,partial,maker,taker,step):
    from scalp_bot.instrument import InstrumentSpec
    c=settings(tmp_path,profile=ACTIVE_PROFILE).model_copy(update={'partial_take_enabled':partial})
    b=OrderBook([(99.99,2),(99.98,10),(99.97,10000)],[(100.,2),(100.01,10),(100.02,10000)])
    result=RiskEngine(c).build_plan('XUSDT',setup(direction,distance=1.),1000,b,10000,20,
        instrument=InstrumentSpec('XUSDT','Trading',.001,step,step,5,10000,10000,480,50) if step else None,
        fee_schedule=FeeSchedule('XUSDT',maker,taker,'independent_fixture'))
    assert result.allowed,result.reason
    p=result.plan; e=p.strategy_details['economics']; qty=D(str(p.quantity)); sign=D(direction)
    raw=walk_quantity(b.asks if direction>0 else b.bids,qty)
    entry=raw*(1+sign*D(str(c.slippage_bps))/10000)
    assert p.market_entry==pytest.approx(float(entry),abs=1e-8)
    entry_fee=qty*entry*D(str(taker))
    fraction=D(str(e['partialFraction']))
    exits=[(qty*(1-fraction),D(str(p.target)))]
    if fraction: exits.append((qty*fraction,D(str(e['partialPrice']))))
    winner=sum(sign*q*(price-entry)-q*price*D(str(maker)) for q,price in exits)-entry_fee
    assert p.expected_net_profit==pytest.approx(float(winner),abs=1e-7)
    # This book has no prices beyond the structural stop. Independently walk
    # quote notional to check the documented projected-depth stress model.
    levels=b.bids if direction>0 else b.asks
    notional=D(str(e['quantity']))*D(str(e['marketEntry']))
    remaining=notional; base=D(0)
    for price,size in levels:
        price=D(str(price)); take=min(remaining,price*D(str(size)))
        base+=take/price; remaining-=take
        if remaining==0: break
    assert remaining==0
    vwap=notional/base; best=D(str(levels[0][0]))
    impact=max(D(0),sign*(best-vwap)/best)*D(str(c.stop_depth_stress_multiplier))
    stop=D(str(p.stop))*(1-sign*D(str(c.slippage_bps))/10000)*(1-sign*impact)
    loser=sign*qty*(entry-stop)+entry_fee+qty*stop*D(str(taker))
    assert e['allInNetLossUsd']==pytest.approx(float(loser),abs=1e-7)
    assert p.net_reward_risk==pytest.approx(float(winner/loser),abs=1e-7)
    assert e['breakEvenWinRateTargetOrStop']==pytest.approx(float(loser/(loser+winner)))
    assert float(loser)<=12.5 and qty*abs(D(str(p.stop))-entry)<=D('5.00000001')


@pytest.mark.parametrize('direction',[1,-1])
def test_active_policy_accepts_one_to_one_but_rejects_negative_net(tmp_path,direction):
    c=settings(tmp_path,profile=ACTIVE_PROFILE)
    b=OrderBook([(100.,10000)],[(100.,10000)])
    found=False
    for distance in [.54,.55,.56,.57,.58,.59,.60]:
        d=setup(direction,distance=distance)
        old=RiskEngine(settings(tmp_path)).build_plan('X',deepcopy(d),1000,b,10000,20)
        new=RiskEngine(c).build_plan('X',d,1000,b,10000,20)
        if new.allowed and not old.allowed:
            found=True
            assert 1.<=new.plan.net_reward_risk<1.15
            assert new.plan.target==d.target  # No synthetic target extension.
    assert found
    assert not RiskEngine(c).build_plan('X',setup(direction,distance=.05),1000,b,10000,20).allowed


@pytest.mark.parametrize('side',[Action.LONG,Action.SHORT])
def test_retired_structural_zone_is_not_resurrected_by_raw_detector(monkeypatch,side):
    from scalp_bot.strategy import liquidity
    from scalp_bot.strategy.structure import StructuralLevel, MarketStructure
    from test_liquidity import quiet_rows
    level=StructuralLevel('resistance' if side==Action.LONG else 'support',
        101. if side==Action.LONG else 98.,102. if side==Action.LONG else 99.,4,'1m',.8,lifecycle='broken')
    monkeypatch.setattr(liquidity,'detect_level_zones',lambda *a,**k:[level.as_zone()])
    structure=MarketStructure(levels=[level])
    assert not liquidity.find_liquidity_targets(quiet_rows(),100.,side,min_distance_pct=.001,
        structure=structure,unconsumed_swings_only=True)
    level.lifecycle='worked'
    assert liquidity.find_liquidity_targets(quiet_rows(),100.,side,min_distance_pct=.001,
        structure=structure,unconsumed_swings_only=True)
