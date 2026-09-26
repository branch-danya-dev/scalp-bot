"""Real playbook events under real routing, risk, accounting and protection."""
from dataclasses import replace
from time import time

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, TradeTick, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy.market_context import build_structure_context
from scalp_bot.strategy.regime import LocalRegime
from test_price_action_hypothesis import scenario as context_fixture
from test_strategies import weak_support_rejection_candles, rejection_absorption_only_flow
from test_strategy_level_semantics import young_support
from test_trend_structure import long_pullback_candles, structure as trend_structure, buy_flow


@pytest.mark.parametrize('owner', ['weak_level_rejection', 'trend_structure'])
@pytest.mark.parametrize('direction', [1, -1])
async def test_actual_owner_event_opens_and_protects_position(tmp_path, owner, direction):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=False, trend_structure_enabled=True,
        # Admission economics are tested separately; retain fees and absolute
        # net-R floor, stops and default cash/exposure limits in this positive path.
        min_net_profit_usd=0, min_net_profit_equity_fraction=0))
    try:
        now = 20_002_200
        rows = weak_support_rejection_candles() if owner=='weak_level_rejection' else long_pullback_candles()
        structure = young_support() if owner=='weak_level_rejection' else trend_structure('support')
        ticks = rejection_absorption_only_flow() if owner=='weak_level_rejection' else [replace(t, ts_ms=t.ts_ms+19_901_700) for t in buy_flow()]
        if direction<0:
            rows = [replace(c, open=200-c.open, high=200-c.low, low=200-c.high, close=200-c.close) for c in rows]
            ticks = [replace(t, price=200-t.price, side='Buy' if t.side=='Sell' else 'Sell') for t in ticks]
            for level in structure.levels:
                level.kind='resistance'; level.low,level.high=200-level.high,200-level.low
            structure = structure if owner=='weak_level_rejection' else trend_structure('resistance')
        context = context_fixture(direction)[2]
        context = replace(context, observed_at_ms=now, forming_candle=None,
            local_regime=replace(context.local_regime, regime=LocalRegime.PULLBACK if owner=='trend_structure' else LocalRegime.RANGE))
        session=ActiveSymbolSession('TESTUSDT', candles=rows, structure=structure,
            last_market_at=time(), last_book_at=time(), book_synced=True)
        engine.sessions[session.symbol]=session
        mids=[100.095,100.12] if owner=='weak_level_rejection' else [100.10,100.115,100.14]
        for i, mid in enumerate(mids):
            mid=mid if direction>0 else 200-mid
            session.orderbook=OrderBook([(mid-.005,10000)],[(mid+.005,10000)])
            session.last_price=mid
            session.market_context=replace(context, observed_at_ms=now+i*1000,last_price=mid,
                structure=build_structure_context(structure,mid))
            selected=engine._route_scenario(session,rows)
            assert selected and selected.owner==owner
            d=engine.strategies[owner].evaluate(rows,session.orderbook,Trend.UP if direction>0 else Trend.DOWN,
                symbol=session.symbol,trades=ticks,structure=structure,market_context=session.market_context,
                observed_at_ms=now+i*1000)
            d=engine._scenario_decision(session,d)
            session.decisions[owner]=d
            if i<len(mids)-1:
                assert d.action==Action.WAIT, d.reasons
        assert d.tradeable, d.reasons
        engine.running=True
        engine._arbitrate_once()
        assert session.symbol in engine.broker.positions, [(r['event'], r['payload'].get('reason')) for r in engine.events]
        pos=engine.broker.positions[session.symbol]
        assert pos.strategy==owner and pos.entry_fee_total_usd>0
        engine.toggle_strategy(owner,False)
        # A new hard stop acts immediately even if the owner switch is off.
        stop=pos.stop-direction*.02
        session.orderbook=OrderBook([(stop-.001,10000)],[(stop+.001,10000)])
        engine._mark_position_from_book(session)
        assert not engine.broker.positions
        assert engine.broker.closed_trades[-1]['reason']=='stop'
        assert engine.router.scenarios[session.symbol].state=='COMPLETED'
    finally:
        await engine.close()


async def test_competing_symbols_reserve_current_portfolio_budget_once(tmp_path):
    from scenario_support import assigned
    from scalp_bot.domain import StrategyDecision
    engine=TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=False, max_total_risk_fraction=.005,
        min_net_profit_usd=0, min_net_profit_equity_fraction=0))
    try:
        for symbol in ('AAA','BBB'):
            session=ActiveSymbolSession(symbol, orderbook=OrderBook([(100,10000)],[(100.01,10000)]),
                last_price=100, last_book_at=time(), last_market_at=time(), book_synced=True)
            d=StrategyDecision('level_breakout',Action.LONG,[],entry=100.01,stop=99.5,target=102,
                setup_id=symbol,details={'riskScale':.65})
            session.decisions[d.strategy]=d
            engine.sessions[symbol]=session
            s=assigned(engine,session,d.strategy)
            s.frozen={'riskScale':.65,'budget':2,'entryArea':[99.9,100.1]}
            a=engine._owned_assessment(session,d)
            b=engine._owned_assessment(session,d)
            assert a.risk_scale==b.risk_scale==.65
        engine.running=True
        engine._arbitrate_once()
        engine._arbitrate_once()
        assert engine.broker.positions
        assert engine.broker.open_risk_usd <= engine.broker.balance*.005+1e-6
        for position in engine.broker.positions.values():
            sizing=position.strategy_details['economics']['sizing']
            assert sizing['strategyScale']==.65
            assert sizing['afterStrategyScaleNotionalUsd'] <= sizing['initialRiskNotionalUsd']*.65+1e-6
    finally:
        await engine.close()


@pytest.mark.parametrize('pending', [False,True])
async def test_scanner_rotation_retains_owned_execution_and_stream(tmp_path,pending):
    from test_engine_lifecycle import plan
    from scenario_support import assigned
    engine=TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path), exchange_clock_enabled=False))
    try:
        session=ActiveSymbolSession('AAA',orderbook=OrderBook([(100,10000)],[(100.01,10000)]),
            last_price=100,activated_at=1,last_ranked_at=1)
        engine.sessions['AAA']=session
        original=assigned(engine,session,'level_breakout')
        p=plan('AAA');p.strategy='level_breakout';p.strategy_details['scenario']=original.public()
        if pending:
            p.entry_mode='maker_limit'
            engine.broker.place_pending(p)
        else:
            engine.broker.open(p,session.orderbook)
            engine.toggle_strategy('level_breakout',False)
        stop_event=__import__('asyncio').Event()
        worker=__import__('asyncio').create_task(stop_event.wait())
        engine._worker_tasks['AAA']=(worker,stop_event)
        await engine._cleanup_active_symbols(time()+10000)
        assert engine.sessions['AAA'] is session
        assert engine._worker_tasks['AAA'][0] is worker and not worker.cancelled()
        assert engine.router.scenarios['AAA'].owner=='level_breakout'
    finally:
        await engine.close()


@pytest.mark.parametrize('direction',[1,-1])
async def test_actual_breakout_router_opens_both_sides_on_response(tmp_path,direction):
    from test_breakout_hourly_context import native_inputs, hourly_context
    from breakout_fixtures import breakout_executions
    action=Action.LONG if direction>0 else Action.SHORT
    rows,structure,ticks=native_inputs(action)
    engine=TradingEngine(Settings(_env_file=None,session_dir=str(tmp_path),exchange_clock_enabled=False,
        min_net_profit_usd=0,min_net_profit_equity_fraction=0))
    try:
        mid=100.165 if direction>0 else 99.835
        session=ActiveSymbolSession('TESTUSDT',candles=rows,structure=structure,
            orderbook=OrderBook([(mid-.005,10000)],[(mid+.005,10000)]),last_price=mid,
            last_market_at=time(),last_book_at=time(),book_synced=True)
        engine.sessions[session.symbol]=session
        for now,extra in [(30_004_600,[]),(30_010_000,breakout_executions(30_010_000,short=direction<0))]:
            session.market_context=replace(hourly_context(action),observed_at_ms=now,last_price=mid)
            selected=engine._route_scenario(session,rows)
            assert selected and selected.owner=='level_breakout'
            d=engine.strategies[selected.owner].evaluate(rows,session.orderbook,Trend.FLAT,
                symbol=session.symbol,trades=ticks+extra,structure=structure,
                market_context=session.market_context,observed_at_ms=now)
            d=engine._scenario_decision(session,d)
            session.decisions[d.strategy]=d
        assert d.action==action,d.reasons
        assert d.details['breakHoldSeconds']<8
        engine.running=True
        engine._arbitrate_once()
        assert session.symbol in engine.broker.positions,[(e['event'],e['payload'].get('reason')) for e in engine.events]
        assert engine.broker.positions[session.symbol].side.value==action.value
    finally:
        await engine.close()
