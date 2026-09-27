from copy import deepcopy
from dataclasses import replace

import pytest

from scalp_bot.domain import Action, Side, Trend
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
from scalp_bot.strategy.playbook_context import PlaybookKind, assess_entry_context
from scalp_bot.strategy.regime import HTFBias, LocalRegime
from test_playbook_context import context, strongly_bearish_flow, short_term_bullish_reversal_flow
from test_breakout_hourly_context import hourly_context


def routed(ctx, owner="level_breakout", side="long"):
    return replace(ctx, scenario={"owner": owner, "side": side, "episodeKey": "immutable"})


@pytest.mark.parametrize("playbook,owner", [
    (PlaybookKind.LEVEL_BREAKOUT, "level_breakout"),
    (PlaybookKind.LEVEL_REJECTION, "weak_level_rejection"),
])
def test_scenario_does_not_bypass_opposing_flow(playbook, owner):
    ctx = context(LocalRegime.BEARISH_TREND, direction=Trend.DOWN, parent=Trend.DOWN,
                  flow=strongly_bearish_flow())
    plain = assess_entry_context(playbook, Action.LONG, ctx, Trend.FLAT)
    assigned = assess_entry_context(playbook, Action.LONG, routed(ctx, owner), Trend.FLAT)
    assert not assigned.allowed
    assert assigned.blockers == plain.blockers
    assert assigned.flow_classification == plain.flow_classification


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("confirmed", [False, True])
def test_scenario_preserves_hourly_override_requirements(action, confirmed):
    ctx = hourly_context(action)
    plain = assess_entry_context(PlaybookKind.LEVEL_BREAKOUT, action, ctx, Trend.FLAT,
                                 breakout_confirmed=confirmed)
    assigned = assess_entry_context(PlaybookKind.LEVEL_BREAKOUT, action,
        routed(ctx, side=action.value), Trend.FLAT, breakout_confirmed=confirmed)
    assert assigned.allowed == plain.allowed
    assert assigned.htf_override == plain.htf_override
    assert assigned.blockers == plain.blockers


def test_scenario_rejection_still_checks_local_and_longer_flow():
    ctx = context(LocalRegime.BEARISH_TREND, direction=Trend.DOWN, parent=Trend.DOWN,
                  flow=short_term_bullish_reversal_flow())
    result = assess_entry_context(PlaybookKind.LEVEL_REJECTION, Action.LONG,
        routed(ctx, "weak_level_rejection"), Trend.FLAT)
    assert "rejection_local_and_longer_flow_opposed" in result.blockers


@pytest.mark.parametrize("owner,side", [("trend_structure", "long"), ("level_breakout", "short")])
def test_assignment_identity_is_an_additional_constraint(owner, side):
    ctx = hourly_context(Action.LONG)
    result = assess_entry_context(PlaybookKind.LEVEL_BREAKOUT, Action.LONG,
        routed(ctx, owner, side), Trend.FLAT, breakout_confirmed=True)
    assert not result.allowed
    assert any(b.startswith("scenario_") for b in result.blockers)


@pytest.mark.parametrize("factory,reason", [
    (LevelBreakoutStrategy, "breakout_context_lost"),
    (WeakLevelRejectionStrategy, "weak_level_context_lost"),
])
def test_owned_position_loses_context_only_after_persistent_fresh_evidence(factory, reason):
    strategy = factory()
    ctx = context(LocalRegime.BEARISH_TREND, direction=Trend.DOWN, parent=Trend.DOWN,
                  htf_bias=HTFBias.BEARISH, flow=strongly_bearish_flow())
    identity = {"owner": strategy.key, "side": "long", "episodeKey": "immutable"}
    details = {"scenario": deepcopy(identity), "tradeMode": "trend_following"}
    def manage(at, current):
        return strategy.manage_position(side=Side.LONG, unrealized_pnl=-1, opened_at=90,
            strategy_details=details, decision=None, trend=Trend.UP, last_price=100,
            market_context=replace(current, observed_at_ms=at), observed_at_ms=at)
    assert manage(100_000, ctx) is None
    assert manage(100_001, ctx) is None
    # A transient loss cannot survive a restored policy observation.
    good = replace(ctx, flow=None, htf_bias=None)
    assert manage(101_000, good) is None
    assert manage(102_000, ctx) is None
    assert manage(105_001, ctx) == reason
    assert details["scenario"] == identity


def test_stale_context_does_not_trigger_owned_context_exit():
    strategy = LevelBreakoutStrategy()
    ctx = context(LocalRegime.BEARISH_TREND, direction=Trend.DOWN, parent=Trend.DOWN,
                  htf_bias=HTFBias.BEARISH, flow=strongly_bearish_flow())
    details = {"scenario": {"owner": strategy.key}}
    for at in (100_000, 110_000):
        assert strategy.manage_position(side=Side.LONG, unrealized_pnl=-1, opened_at=90,
            strategy_details=details, decision=None, trend=Trend.UP, last_price=100,
            market_context=replace(ctx, execution=replace(ctx.execution, book_fresh=False)),
            observed_at_ms=at) is None


@pytest.mark.asyncio
async def test_final_dispatch_rechecks_changed_context(tmp_path):
    from scalp_bot.domain import StrategyDecision
    from scalp_bot.engine import ActiveSymbolSession
    from test_engine_lifecycle import make_engine, book
    from scenario_support import assigned
    engine = make_engine(tmp_path, exchange_clock_enabled=False)
    try:
        session = ActiveSymbolSession("AAAUSDT", orderbook=book(), trend=Trend.UP)
        engine.sessions[session.symbol] = session
        assigned(engine, session, "level_breakout")
        session.market_context = context(LocalRegime.BEARISH_TREND, direction=Trend.DOWN,
            parent=Trend.DOWN, flow=strongly_bearish_flow())
        decision = StrategyDecision("level_breakout", Action.LONG, [],
            entry=100, stop=99, target=102, details={"entryContextAssessment": {"allowed": True}})
        assert not engine._scenario_entry_valid(session, decision)
    finally:
        await engine.close()
