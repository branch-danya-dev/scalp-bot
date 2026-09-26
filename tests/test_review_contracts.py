"""R01-R05 regressions using production types, without market connections."""
from copy import deepcopy
from dataclasses import replace

import pytest

from scalp_bot.domain import Action, Candle, OrderBook, Side, StrategyDecision
from scalp_bot.paper import PaperBroker
from scalp_bot.risk import RiskEngine
from scalp_bot.scenario import ScenarioRouter
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.regime import LocalRegime
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel
from test_scenario_router import market
from test_risk import economic_settings


def mature_market(direction=1):
    rows, book, context, structure = market("breakout", direction)
    level = structure.levels[0]
    level.touches = 6
    level.distinct_approaches = 5
    level.lifecycle = "worked"
    level.reaction_pct = .003
    return rows, book, context, structure


@pytest.mark.parametrize("direction", [1, -1])
def test_r01_scenario_risk_prices_the_same_runner_as_paper(direction):
    cfg = economic_settings(
        _env_file=None, enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False, enforce_winner_cost_share_gate=False,
        enforce_stop_cost_share_gate=False, min_net_profit_usd=0,
        min_net_profit_equity_fraction=0, breakout_partial_take_fraction=.30,
        partial_take_at_r=1.0, runner_target_r=2.5, maker_fee_rate=.0002,
    )
    book = OrderBook([(99.99 if direction > 0 else 100, 10000)],
                     [(100 if direction > 0 else 100.01, 10000)])
    d = StrategyDecision("level_breakout", Action.LONG if direction > 0 else Action.SHORT,
        [], entry=100, stop=100-direction*.4, target=100+direction*.49,
        details={"allowRunner": True, "targetSource": "observed_range_projection",
                 "scenario": {"scenarioId": "test:r01", "owner": "level_breakout"}})
    broker = PaperBroker(cfg)
    result = RiskEngine(cfg).build_plan("TESTUSDT", d, 1000, book,
        broker.available_notional, broker.available_risk_usd)
    assert result.allowed, result.reason
    plan = result.plan
    assert plan.strategy_details["economics"]["partialPlanned"]
    pos = broker.open(plan, book)
    # Known resting fills: isolates lifecycle pricing from queue eligibility.
    broker._partial_take(pos, book)
    assert pos.target == plan.target
    trade = broker.close("TESTUSDT", book, "runner_target")
    assert trade["netPnl"] == pytest.approx(plan.expected_net_profit)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("defect", ["immature", "broken"])
def test_r02_router_does_not_assign_untradeable_breakout_object(direction, defect):
    rows, book, context, structure = mature_market(direction)
    level = structure.levels[0]
    if defect == "immature":
        level.touches = level.distinct_approaches = 1
        level.lifecycle = "tested"
    else:
        level.lifecycle = "broken"
    assert not LevelBreakoutStrategy._structural_level_is_tradeable(
        level, rows, long_candidate=direction > 0)
    assert ScenarioRouter().observe("TESTUSDT", context, rows, structure,
        {"level_breakout": True}, 1) is None


@pytest.mark.parametrize("direction", [1, -1])
def test_r03_foreign_level_cannot_freeze_assigned_scenario(direction):
    rows, book, context, structure = mature_market(direction)
    router = ScenarioRouter()
    s = router.observe("TESTUSDT", context, rows, structure, {"level_breakout": True}, 1)
    assert s is not None
    wrong = deepcopy(structure.levels[0]); wrong.generation_id = "different-object"
    entry = book.executable_entry(Side(s.side))
    d = StrategyDecision(s.owner, Action(s.side), [], entry=entry,
        stop=entry-direction*.3, target=entry+direction*.8,
        details={"levelLifecycle": wrong.public(), "zoneGeneration": [wrong.kind, wrong.generation_id]},
        setup_id="foreign:g1")
    assert not router.accept_decision(s.symbol, d, 2, book).tradeable
    assert s.frozen is None


def test_r04_completion_does_not_consume_unselected_market_object():
    rows, book, context, structure = mature_market()
    near = structure.levels[0]
    far = deepcopy(near)
    far.low += .10; far.high += .10; far.generation_id = "other-level"
    structure.levels.append(far)
    router = ScenarioRouter(); enabled = {"level_breakout": True}
    s = router.observe("TESTUSDT", context, rows, structure, enabled, 1)
    assert s is not None and s.level["generation_id"] == near.generation_id
    router.completed(s.symbol, 2, "target")
    other = router.observe(s.symbol, context, rows, structure, enabled, 3)
    assert other is not None and other.level["generation_id"] == far.generation_id


def test_r05_closing_the_sweep_candle_is_not_a_new_failed_break():
    from scalp_bot.strategy.pre_state import build_forming_candle_context
    rows, book, context, _ = market("rejection")
    level = StructuralLevel("support", 100.09, 100.11, 1, "1m", 1,
        generation_id="support:g1", distinct_approaches=1, lifecycle="tested")
    # Only the forming candle has the sweep; older closed candle does not.
    rows[-1] = replace(rows[-1], low=100.12, high=100.24, open=100.2, close=100.23)
    start = context.observed_at_ms // 60000 * 60000
    sweep = Candle(start, 100.20, 100.25, 100.05, 100.23, 100, 10000, False)
    forming = build_forming_candle_context(sweep, rows, observed_at_ms=context.observed_at_ms)
    context = replace(context, forming_candle=forming, last_price=100.23,
        local_regime=replace(context.local_regime, regime=LocalRegime.RANGE))
    router = ScenarioRouter(); enabled = {"weak_level_rejection": True}
    structure = MarketStructure([level])
    s = router.observe("TESTUSDT", context, rows, structure, enabled, 1)
    assert s is not None
    router.completed(s.symbol, 2, "target")
    assert router.observe(s.symbol, context, rows, structure, enabled, 3) is None
    later = start+60000+1000
    closed_rows = rows+[replace(sweep, confirmed=True)]
    unswept = Candle(start+60000, 100.23, 100.24, 100.20, 100.23, 100, 10000, False)
    forming = build_forming_candle_context(unswept, closed_rows, observed_at_ms=later)
    later_context = replace(context, observed_at_ms=later, forming_candle=forming)
    assert router.observe(s.symbol, later_context, closed_rows, structure, enabled, 4) is None


@pytest.mark.parametrize("direction", [1, -1])
def test_r01_hard_floor_cannot_be_passed_with_a_fictional_runner(direction):
    cfg = economic_settings(_env_file=None, absolute_min_net_reward_risk=1,
        enforce_min_net_profit_gate=False, enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=False, enforce_stop_cost_share_gate=False,
        breakout_partial_take_fraction=.30, partial_take_at_r=1, runner_target_r=2.5)
    book = OrderBook([(99.99 if direction > 0 else 100, 10000)],
                     [(100 if direction > 0 else 100.01, 10000)])
    d = StrategyDecision("level_breakout", Action.LONG if direction > 0 else Action.SHORT,
        [], entry=100, stop=100-direction*.4, target=100+direction*.49,
        details={"allowRunner": True, "targetSource": "observed_range_projection",
                 "scenario": {"scenarioId": "r01:hard-floor"}})
    result = RiskEngine(cfg).build_plan("TESTUSDT", d, 1000, book, 10000, 20)
    assert not result.allowed
    assert "absolute_net_reward_risk" in result.reason
    assert result.diagnostics["netRewardRisk"] < 1
    assert result.diagnostics["runnerTargetPct"] == pytest.approx(
        result.diagnostics["targetMovePct"])


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("kind", ["horizontal", "session_extreme"])
def test_r02_eligible_breakout_objects_still_get_an_owner(direction, kind):
    rows, book, context, structure = mature_market(direction)
    if kind == "session_extreme":
        level = structure.levels[0]
        level.kind = "day_high" if direction > 0 else "day_low"
        level.touches = level.distinct_approaches = 1
        level.lifecycle = "fresh"
    s = ScenarioRouter().observe("TESTUSDT", context, rows, structure,
        {"level_breakout": True}, 1)
    assert s and s.owner == "level_breakout"
    assert s.object_id == "level:" + structure.levels[0].generation_id


@pytest.mark.parametrize("direction", [1, -1])
def test_r03_actual_breakout_uses_assigned_level_not_closer_competitor(direction):
    from scalp_bot.domain import Trend
    from scalp_bot.strategy.scenario_objects import decision_object_id, level_object_id
    from test_breakout_hourly_context import native_inputs, hourly_context
    from breakout_fixtures import breakout_executions
    action = Action.LONG if direction > 0 else Action.SHORT
    rows, structure, ticks = native_inputs(action)
    assigned = structure.levels[0]
    competitor = deepcopy(assigned)
    competitor.generation_id = "closer-unassigned-level"
    competitor.level_id = "closer-unassigned"
    competitor.low += direction*.07
    competitor.high += direction*.07
    structure.levels.append(competitor)
    obstacle = deepcopy(assigned)
    obstacle.generation_id = "unassigned-target-obstacle"
    obstacle.low += direction*.65
    obstacle.high += direction*.65
    structure.levels.append(obstacle)
    strategy = LevelBreakoutStrategy()
    mid = 100.165 if direction > 0 else 99.835
    book = OrderBook([(mid-.005, 10000)], [(mid+.005, 10000)])
    contract = {"owner": strategy.key, "side": action.value, "state": "ASSIGNED",
                "scenarioId": "R03", "marketObjectId": level_object_id(assigned)}
    for now, extra in [(30_004_600, []),
                       (30_010_000, breakout_executions(30_010_000, short=direction<0))]:
        context = replace(hourly_context(action), observed_at_ms=now,
                          last_price=mid, scenario=contract)
        d = strategy.evaluate(rows, book, Trend.FLAT, symbol="TESTUSDT",
            trades=ticks+extra, structure=structure, market_context=context, observed_at_ms=now)
    assert d.tradeable, d.reasons
    assert decision_object_id(d) == level_object_id(assigned)
    # Entry object is scoped; the other structural object still participates in
    # target/obstacle analysis. The full structure was not replaced by a singleton.
    obstacle_price = obstacle.low if direction>0 else obstacle.high
    assert any(row["price"] == pytest.approx(obstacle_price)
               for row in d.details["liquidityLadder"]), d.details["liquidityLadder"]


@pytest.mark.parametrize("direction", [1, -1])
def test_r03_conflicting_or_missing_object_metadata_is_not_self_authenticated(direction):
    from test_scenario_router import decision
    rows, book, context, structure = mature_market(direction)
    for mode in ("missing", "conflicting"):
        r = ScenarioRouter()
        s = r.observe("TESTUSDT", context, rows, structure, {"level_breakout": True}, 1)
        d = decision(s, book)
        if mode == "missing":
            d.details.pop("levelLifecycle")
        else:
            d.details["zoneGeneration"] = [structure.levels[0].kind, "FOREIGN"]
        d.details["scenario"] = s.public()  # merely copying router metadata is insufficient
        assert not r.accept_decision(s.symbol, d, 2, book).tradeable
        assert s.frozen is None


def test_r03_trendline_projection_changes_preserve_identity_but_other_anchor_does_not():
    from scalp_bot.domain import Trend
    from scalp_bot.strategy.trend_structure import TrendStructureStrategy
    from scalp_bot.strategy.scenario_objects import trendline_object_id, decision_object_id
    from test_trend_structure import long_pullback_candles, structure as trend_structure, buy_flow
    rows = long_pullback_candles()
    structure = trend_structure("support")
    assigned = structure.trendlines[0]
    competitor = replace(assigned, start_ms=assigned.start_ms+60_000, score=assigned.score+100)
    structure.trendlines.append(competitor)
    context = market("trend")[2]
    contract = {"owner": "trend_structure", "side": "long", "state": "ASSIGNED",
                "marketObjectId": trendline_object_id(assigned)}
    assert trendline_object_id(replace(assigned, end_ms=assigned.end_ms+60_000,
        current_price=assigned.current_price+.01)) == trendline_object_id(assigned)
    context = replace(context, forming_candle=None, scenario=contract)
    d = TrendStructureStrategy().evaluate(rows, OrderBook([(100.095,10000)],[(100.105,10000)]),
        Trend.UP, symbol="TESTUSDT", trades=buy_flow(), structure=structure,
        market_context=context, observed_at_ms=context.observed_at_ms)
    assert decision_object_id(d) == trendline_object_id(assigned), d.details


@pytest.mark.parametrize("direction", [1, -1])
def test_r03_beta_cannot_trade_a_replacement_closed_candle(direction):
    from scalp_bot.domain import Trend
    from scalp_bot.strategy.price_action_hypothesis import PriceActionHypothesisStrategy
    from scalp_bot.strategy.scenario_objects import candle_object_id
    from test_price_action_hypothesis import scenario as beta_market
    rows, book, context, flow = beta_market(direction)
    contract = {"owner": "price_action_hypothesis", "side": "long" if direction>0 else "short",
                "state": "ASSIGNED", "marketObjectId": candle_object_id(rows[-1].start_ms-60_000)}
    context = replace(context, scenario=contract)
    d = PriceActionHypothesisStrategy().evaluate(rows, book, Trend.FLAT, symbol="TESTUSDT",
        market_context=context, observed_at_ms=context.observed_at_ms, trade_flow=flow)
    assert not d.tradeable
    assert "assigned candle object" in d.reasons[0]


def test_r04_risk_rejection_does_not_enable_same_object_other_strategy():
    rows, book, context, structure = mature_market()
    # A session boundary is eligible for both playbooks, but unchanged market
    # context alone is not a new basis after an economic rejection expires.
    structure.levels[0].kind = "day_high"
    context = replace(context, local_regime=replace(context.local_regime, regime=LocalRegime.RANGE))
    r = ScenarioRouter(); enabled = {"level_breakout": True, "weak_level_rejection": True}
    s = r.observe("TESTUSDT", context, rows, structure, enabled, 1)
    assert s and s.owner == "level_breakout"
    r.reject(s.symbol, "risk", "cost floor", 2)
    assert r.observe(s.symbol, context, rows, structure, enabled, 3) is s
    r.transition(s, "EXPIRED", 4, "signal expired after economic rejection")
    assert r.observe(s.symbol, context, rows, structure, enabled, 5) is None


@pytest.mark.parametrize("direction", [1, -1])
def test_r05_real_quote_sweep_rearms_but_delayed_wick_does_not(direction):
    from scalp_bot.scenario_episodes import FailedBreakEpisodes
    level = StructuralLevel("support" if direction>0 else "resistance",
        99.9, 100.1, 1, "1m", 1, generation_id="event:g1")
    side = "long" if direction>0 else "short"
    transform = lambda p: p if direction>0 else 200-p
    def bar(start, low=100.15):
        c = Candle(start, 100.2, 100.3, low, 100.2, 1, 100, False)
        if direction<0:
            c = replace(c, open=200-c.open, high=200-c.low, low=200-c.high, close=200-c.close)
        return c
    tracker = FailedBreakEpisodes()
    bars = [bar(60_000)]
    assert tracker.observe("L", level, side, transform(100.2), 61_000, bars) is None
    assert tracker.observe("L", level, side, transform(99.8), 62_000, bars) is None
    first = tracker.observe("L", level, side, transform(100.2), 63_000, bars)
    assert first
    # The slower candle message reports the same, already consumed excursion.
    bars = [bar(60_000, low=99.8)]
    assert tracker.observe("L", level, side, transform(100.2), 64_000, bars) == first
    bars = [replace(bars[0], confirmed=True), bar(120_000)]
    assert tracker.observe("L", level, side, transform(100.2), 121_000, bars) == first
    # A new quote crossing in the same currently forming bar IS new evidence.
    assert tracker.observe("L", level, side, transform(99.8), 122_000, bars) is None
    second = tracker.observe("L", level, side, transform(100.2), 123_000, bars)
    assert second and second != first
    bars[-1] = bar(120_000, low=99.8)
    assert tracker.observe("L", level, side, transform(100.2), 124_000, bars) == second
    # Once the event has left the observable bar window it is not resurrected.
    assert tracker.observe("L", level, side, transform(100.2), 241_000,
                           [bar(180_000), bar(240_000)]) is None


@pytest.mark.parametrize("removed", [False, True])
def test_r02_disappeared_or_broken_assigned_level_releases_before_order(removed):
    rows, book, context, structure = mature_market()
    r = ScenarioRouter(); enabled = {"level_breakout": True}
    s = r.observe("TESTUSDT", context, rows, structure, enabled, 1)
    if removed:
        structure.levels.clear()
    else:
        structure.levels[0].lifecycle = "broken"
    assert r.observe(s.symbol, context, rows, structure, enabled, 2) is None
    assert s.state == "INVALIDATED"
    assert s.last_transition_reason == "assigned_market_object_unavailable"


@pytest.mark.parametrize("direction", [1, -1])
def test_r03_rejection_prepares_only_assigned_support_or_resistance(direction):
    from scalp_bot.domain import Trend
    from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
    from scalp_bot.strategy.scenario_objects import level_object_id
    from test_strategies import weak_support_rejection_candles, rejection_absorption_only_flow
    from test_strategy_level_semantics import young_support
    rows = weak_support_rejection_candles(); ticks = rejection_absorption_only_flow()
    structure = young_support(); assigned = structure.levels[0]
    if direction < 0:
        rows = [replace(c, open=200-c.open, high=200-c.low, low=200-c.high, close=200-c.close) for c in rows]
        ticks = [replace(t, price=200-t.price, side='Buy' if t.side=='Sell' else 'Sell') for t in ticks]
        assigned.kind='resistance'; assigned.low, assigned.high=200-assigned.high,200-assigned.low
    competitor = deepcopy(assigned)
    competitor.generation_id = "closer-but-unassigned"
    competitor.low += direction*.05; competitor.high += direction*.05
    structure.levels.append(competitor)
    _, _, context, _ = market("rejection", direction)
    context = replace(context, forming_candle=None,
        local_regime=replace(context.local_regime, regime=LocalRegime.RANGE),
        scenario={"owner": "weak_level_rejection", "side": "long" if direction>0 else "short",
                  "state": "ASSIGNED", "marketObjectId": level_object_id(assigned)})
    strategy = WeakLevelRejectionStrategy()
    for i, value in enumerate((100.095, 100.12)):
        mid = value if direction>0 else 200-value
        book = OrderBook([(mid-.005,10000)],[(mid+.005,10000)])
        context = replace(context, observed_at_ms=20_002_200+i*1000, last_price=mid)
        d = strategy.evaluate(rows, book, Trend.UP if direction>0 else Trend.DOWN,
            symbol="TESTUSDT", trades=ticks, structure=structure, market_context=context,
            observed_at_ms=context.observed_at_ms)
    assert d.tradeable, d.reasons
    assert d.details["levelGeneration"] == assigned.generation_id
    assert d.details["zone"]["low"] == assigned.low
