"""Phase-aware expiry of an unprepared owner; real router/strategy, no market IO."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scalp_bot.domain import Action, Side, StrategyDecision
from scalp_bot.scenario import ScenarioRouter
from scalp_bot.strategy.structure import MarketStructure
from test_scenario_remediation import ENABLED, SYMBOL, level, market, ready


def assigned(direction=1):
    a = level('resistance' if direction > 0 else 'support',
              center=100.4 if direction > 0 else 100.0)
    b = level('support' if direction > 0 else 'resistance', key='B', mature=False,
              center=100.0 if direction > 0 else 100.4)
    rows, book, ctx, structure = market([a, b])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert s and s.owner == 'level_breakout' and s.state == 'ASSIGNED'
    return router, s, a, b, rows, book, ctx


@pytest.mark.parametrize('direction', [1, -1])
@pytest.mark.parametrize('change', ['broken', 'unworked'])
def test_unprepared_owner_releases_ineligible_object_and_next_event_selects_other(direction, change):
    router, s, a, b, rows, book, ctx = assigned(direction)
    changed = replace(a, lifecycle='broken' if change == 'broken' else 'tested')
    structure = MarketStructure([changed, b])
    assert router.observe(SYMBOL, ctx, rows, structure, ENABLED, 11) is None
    assert s.state == 'INVALIDATED'
    assert s.last_transition_reason == 'assigned_object_no_longer_preparable'
    assert s.last_rejection['owner'] == 'strategy'
    next_s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 11.01)
    assert next_s and next_s.owner == 'weak_level_rejection'
    assert next_s.object_ref.key == b.generation_id
    assert s.episode_key in router._consumed[SYMBOL]
    assert next_s.episode_key not in router._consumed[SYMBOL]
    assert next_s.assigned_mono == 11.01  # No new cooldown/wait.


@pytest.mark.parametrize('direction', [1, -1])
@pytest.mark.parametrize('missing', ['context', 'not_ready', 'unsynced', 'history', 'structure'])
def test_missing_data_is_not_structural_invalidation(direction, missing):
    router, s, a, b, rows, book, ctx = assigned(direction)
    structure = MarketStructure([replace(a, lifecycle='broken'), b])
    if missing == 'context':
        ctx = None
    elif missing in {'not_ready', 'unsynced'}:
        ctx = replace(ctx, execution=replace(ctx.execution, **(
            {'book_fresh': False} if missing == 'not_ready' else {'book_synced': False})))
    elif missing == 'history':
        rows = rows[-20:]
    else:
        structure = None
    assert router.observe(SYMBOL, ctx, rows, structure, ENABLED, 11) is s
    assert s.state == 'ASSIGNED'


@pytest.mark.parametrize('direction', [1, -1])
def test_minor_distance_change_without_new_candidate_does_not_cancel_applicable_owner(direction):
    router, s, a, b, rows, book, ctx = assigned(direction)
    # Keep object applicable but remove the routing candidacy by thinning the
    # normalization range; its ranking/approach radius is not owner invalidation.
    thin = [replace(c, high=c.close+.02, low=c.close-.02) for c in rows]
    assert router.observe(SYMBOL, ctx, thin, MarketStructure([a, b]), ENABLED, 11) is s


@pytest.mark.parametrize('direction', [1, -1])
@pytest.mark.parametrize('phase', ['PREPARED', 'ARMED', 'ORDER_PENDING', 'IN_POSITION'])
def test_prepared_frozen_pending_and_position_keep_their_own_continuation_contract(direction, phase):
    router, s, a, b, rows, book, ctx = assigned(direction)
    if phase == 'PREPARED':
        d = StrategyDecision(s.owner, Action.WAIT, [], details={'state': 'armed'})
        router.accept_decision(SYMBOL, d, 10.2, book)
    else:
        assert router.accept_decision(SYMBOL, ready(s, book, a), 10.2, book).tradeable
    kwargs = {}
    if phase == 'ORDER_PENDING':
        router.submitted(SYMBOL, 10.3)
        kwargs['pending'] = SimpleNamespace(plan=SimpleNamespace(
            strategy=s.owner, side=Side(s.side), strategy_details={'scenario': s.public()}))
    elif phase == 'IN_POSITION':
        router.filled(SYMBOL, 10.3)
        kwargs['position'] = SimpleNamespace(
            strategy=s.owner, side=Side(s.side), strategy_details={'scenario': s.public()})
    else:
        router.reject(SYMBOL, 'risk', 'unchanged risk refusal', 10.3)
    frozen, deadline, identity = deepcopy(s.frozen), s.expires_mono, s.scenario_id
    assert router.observe(SYMBOL, ctx, rows, MarketStructure([replace(a, lifecycle='broken'), b]),
                          ENABLED, 11, **kwargs) is s
    assert (s.frozen, s.expires_mono, s.scenario_id) == (frozen, deadline, identity)
    assert s.state == phase


@pytest.mark.parametrize('direction', [1, -1])
def test_rejection_losing_object_applicability_before_preparation_is_released(direction):
    a = level('support' if direction > 0 else 'resistance', mature=False,
              center=100 if direction > 0 else 100.4)
    rows, book, ctx, structure = market([a])
    router = ScenarioRouter()
    enabled = {'weak_level_rejection': True}
    s = router.observe(SYMBOL, ctx, rows, structure, enabled, 10)
    assert s and s.owner == 'weak_level_rejection'
    changed = replace(a, distinct_approaches=5, touches=6, lifecycle='worked')
    assert router.observe(SYMBOL, ctx, rows, MarketStructure([changed]), enabled, 11) is None
    assert s.last_transition_reason == 'assigned_object_no_longer_preparable'


@pytest.mark.parametrize('direction', [1, -1])
def test_trendline_losing_touch_support_before_preparation_is_released(direction):
    from test_scenario_router import assign
    router, s, rows, book, ctx, st, enabled = assign('trend', direction)
    changed = replace(st.trendlines[0], touches=2)
    assert router.observe(s.symbol, ctx, rows, MarketStructure([], [changed]), enabled, 11) is None
    assert s.last_transition_reason == 'assigned_object_no_longer_preparable'


@pytest.mark.parametrize('action', [Action.LONG, Action.SHORT])
def test_real_pinned_breakout_survives_broken_lifecycle_and_fires_without_new_hold(action):
    from breakout_fixtures import breakout_executions
    from test_breakout_hourly_context import native_inputs, hourly_context
    from scalp_bot.domain import OrderBook, Trend
    from scalp_bot.strategy.breakout import LevelBreakoutStrategy
    rows, st, ticks = native_inputs(action)
    long = action == Action.LONG
    price = 100.165 if long else 99.835
    ticks = [replace(t, price=100.16 if long else 99.84) for t in ticks]
    book = OrderBook([(price-.005, 50)], [(price+.005, 50)])
    ctx = replace(hourly_context(action), observed_at_ms=30_004_600,
                  last_price=price, forming_candle=None)
    router, strategy = ScenarioRouter(), LevelBreakoutStrategy()
    enabled = {'level_breakout': True}
    s = router.observe('PIN', ctx, rows, st, enabled, 10)
    assert s
    ctx = replace(ctx, scenario=s.public())
    d = strategy.evaluate(rows, book, Trend.FLAT, symbol='PIN', trades=ticks,
                          structure=st, market_context=ctx, observed_at_ms=30_004_600)
    assert not d.tradeable and strategy._states['PIN'].armed_zone is not None
    router.accept_decision('PIN', d, 10.1, book)
    assert s.state == 'PREPARED'
    st.levels[0].lifecycle = 'broken'
    assert not strategy.can_prepare(st.levels[0], rows, price, action.value)
    ctx = replace(ctx, observed_at_ms=30_010_000)
    assert router.observe('PIN', ctx, rows, st, enabled, 15.4) is s
    d = strategy.evaluate(rows, book, Trend.FLAT, symbol='PIN',
                          trades=ticks+breakout_executions(30_010_000, short=not long),
                          structure=st, market_context=replace(ctx, scenario=s.public()),
                          observed_at_ms=30_010_000)
    assert d.action == action, d.reasons
    assert d.details['breakHoldSeconds'] < strategy.hold_without_retest_seconds
    assert router.accept_decision('PIN', d, 15.4, book).tradeable
    assert s.state == 'ARMED'


@pytest.mark.parametrize('direction', [1, -1])
async def test_engine_releases_empty_owner_and_routes_other_object_on_next_event(tmp_path, direction):
    from scalp_bot.config import Settings
    from scalp_bot.engine import ActiveSymbolSession, TradingEngine
    from scalp_bot.domain import Trend
    router, s, a, b, rows, book, ctx = assigned(direction)
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
                                   exchange_clock_enabled=False))
    try:
        # Use the engine's monotonic clock rather than comparing it with the
        # explicit logical seconds used in isolated router tests above.
        session = ActiveSymbolSession(SYMBOL, candles=rows, orderbook=book,
                                      market_context=ctx, structure=MarketStructure([a, b]))
        engine.sessions[SYMBOL] = session
        first = engine._route_scenario(session, rows)
        assert first and first.state == 'ASSIGNED'
        session.structure = MarketStructure([replace(a, lifecycle='broken'), b])
        independent = engine.router.scenario_for(SYMBOL, "weak_level_rejection")
        second = engine._route_scenario(session, rows)
        assert first.state == "INVALIDATED"
        assert second is independent  # Parallel preparation survives immediately.
        assert session.market_context.scenario is None
        assert second and second.owner == 'weak_level_rejection'
        assert second.object_ref.key == b.generation_id
        decision = engine.strategies[second.owner].evaluate(
            rows, book, Trend.FLAT, symbol=SYMBOL, structure=session.structure,
            market_context=engine.router.context_for(session.market_context,SYMBOL,second.owner), trades=[], observed_at_ms=ctx.observed_at_ms)
        assert decision.strategy == second.owner
        transitions = [e['payload'] for e in engine.events if e['event']=='scenario_transition']
        assert any(e['transitionReason']=='assigned_object_no_longer_preparable' for e in transitions)
    finally:
        await engine.close()
