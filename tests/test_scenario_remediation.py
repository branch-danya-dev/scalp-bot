"""R01--R05 regressions on production classes; no adapter models or network."""
from dataclasses import replace

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candle, OrderBook, Side, StrategyDecision, Trend
from scalp_bot.paper import PaperBroker
from scalp_bot.risk import RiskEngine
from scalp_bot.scenario import ScenarioRouter
from scalp_bot.strategy.pre_state import build_forming_candle_context
from scalp_bot.strategy.regime import LocalRegime
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel
from test_price_action_hypothesis import scenario as seed_context


SYMBOL = "REVIEWUSDT"
ENABLED = {"level_breakout": True, "weak_level_rejection": True}


def level(kind="resistance", key="A", *, mature=True, broken=False, center=100.4):
    return StructuralLevel(
        kind, center - .02, center + .02, 6 if mature else 1, "1m", 1,
        reaction_pct=.02, volume_ratio=1.2,
        level_id=key, generation_id=f"{key}:g1",
        distinct_approaches=5 if mature else 1,
        lifecycle="broken" if broken else ("worked" if mature else "tested"),
    )


def market(levels, *, price=100.2, now=6_010_000, forming=None):
    _, _, ctx, _ = seed_context()
    minute = now // 60_000 * 60_000
    rows = [Candle(minute-(70-i)*60_000, 100, 100.5, 99.5, 100, 100, 10000)
            for i in range(70)]
    # No failed break in the most recent closed bar unless explicitly supplied.
    rows[-1] = replace(rows[-1], open=100.2, high=100.3, low=100.15, close=100.2)
    book = OrderBook([(price-.001, 10000)], [(price+.001, 10000)])
    ctx = replace(ctx, symbol=SYMBOL, observed_at_ms=now, last_price=price,
                  local_regime=replace(ctx.local_regime, regime=LocalRegime.RANGE,
                                       direction=Trend.FLAT),
                  forming_candle=build_forming_candle_context(forming, rows, observed_at_ms=now))
    return rows, book, ctx, MarketStructure(levels)


def ready(s, book, actual_level):
    direction = 1 if s.side == "long" else -1
    entry = book.executable_entry(Side(s.side))
    return StrategyDecision(s.owner, Action(s.side), [], entry=entry,
        stop=entry-direction*.4, target=entry+direction*.8,
        setup_id=f"{s.owner}:{s.side}:{actual_level.generation_id}",
        details={"state": "impulse", "zone": actual_level.as_zone().public(),
                 "zoneGeneration": (actual_level.as_zone().kind, actual_level.generation_id),
                 "levelGeneration": actual_level.generation_id,
                 "levelLifecycle": actual_level.public()})


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("structural", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_r01_routed_plan_payoff_matches_actual_partial_and_target(direction, structural, partial):
    cfg = Settings(_env_file=None, min_net_profit_usd=0, min_net_profit_equity_fraction=0,
                   enforce_min_net_profit_gate=False, enforce_net_reward_risk_gate=False,
                   partial_take_enabled=partial, runner_target_r=2.5)
    book = OrderBook([(99.999, 10000)], [(100.001, 10000)])
    entry = book.executable_entry(Side.LONG if direction > 0 else Side.SHORT)
    target = entry + direction*.8
    details = {"scenario": {"scenarioId": "R01"}, "targetSource": "observed_range_projection"}
    if structural:
        details.update(targetSource="liquidity_ladder", liquidityTarget={"price": target})
    d = StrategyDecision("weak_level_rejection", Action.LONG if direction > 0 else Action.SHORT,
                         [], entry=entry, stop=entry-direction*.4, target=target, details=details)
    broker = PaperBroker(cfg)
    result = RiskEngine(cfg).build_plan(SYMBOL, d, broker.balance, book,
                                       broker.available_notional, broker.available_risk_usd)
    assert result.allowed, result.reason
    plan = result.plan
    broker.open(plan, book)
    economics = plan.strategy_details["economics"]
    if partial:
        trigger = economics["firstTakePrice"]
        through = trigger + direction*.01
        events = broker.mark(SYMBOL, through, OrderBook([(through-.001,10000)],[(through+.001,10000)]),
                             trade_price=through, trade_notional_usd=1e9,
                             trade_side="Buy" if direction > 0 else "Sell")
        assert any(e["event"] == "partial_take" for e in events)
        assert broker.positions[SYMBOL].target == plan.target
    through = target + direction*.01
    broker.mark(SYMBOL, through, OrderBook([(through-.001,10000)],[(through+.001,10000)]),
                trade_price=through, trade_notional_usd=1e9,
                trade_side="Buy" if direction > 0 else "Sell")
    trade = broker.closed_trades[-1]
    assert trade["grossPnl"] == pytest.approx(plan.expected_gross_profit, abs=1e-9)
    assert trade["netPnl"] == pytest.approx(plan.expected_net_profit, abs=1e-9)


@pytest.mark.parametrize("direction", [1, -1])
def test_r01_phantom_runner_cannot_rescue_below_one_net_r(direction):
    cfg = Settings(_env_file=None, min_net_profit_usd=0, min_net_profit_equity_fraction=0,
                   enforce_min_net_profit_gate=False, enforce_net_reward_risk_gate=False,
                   runner_target_r=2.5)
    book = OrderBook([(99.999, 10000)], [(100.001, 10000)])
    d = StrategyDecision("weak_level_rejection", Action.LONG if direction > 0 else Action.SHORT,
                         [], entry=100, stop=100-direction*.4, target=100+direction*.48,
                         details={"scenario": {"scenarioId": "R01"}, "targetSource": "observed_range_projection"})
    result = RiskEngine(cfg).build_plan(SYMBOL, d, 1000, book, 10000, 20)
    assert not result.allowed
    assert "absolute_net_reward_risk" in result.reason


@pytest.mark.parametrize("kind", ["support", "resistance"])
@pytest.mark.parametrize("broken", [False, True])
def test_r02_router_never_assigns_ineligible_horizontal_breakout(kind, broken):
    obj = level(kind, mature=False, broken=broken,
                center=100.0 if kind == "support" else 100.4)
    rows, book, ctx, structure = market([obj])
    s = ScenarioRouter().observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    if broken:
        assert s is None
    else:
        assert s is not None and s.owner == "weak_level_rejection"


@pytest.mark.parametrize("kind", ["support", "resistance"])
def test_r02_mature_level_remains_available_for_breakout(kind):
    obj = level(kind, center=100 if kind == "support" else 100.4)
    rows, book, ctx, structure = market([obj])
    s = ScenarioRouter().observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert s and s.owner == "level_breakout"


@pytest.mark.parametrize("kind", ["day_high", "previous_day_high", "day_low", "previous_day_low"])
def test_r02_session_extreme_does_not_require_manufactured_maturity(kind):
    obj = level(kind, mature=False, center=100 if kind.endswith("low") else 100.4)
    rows, book, ctx, structure = market([obj])
    s = ScenarioRouter().observe(SYMBOL, ctx, rows, structure, {"level_breakout":True}, 10)
    assert s and s.owner == "level_breakout"


def test_r03_same_owner_cannot_freeze_a_different_level():
    a, b = level(key="A"), level(key="B", center=100.5)
    rows, book, ctx, structure = market([a, b])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert s and s.level["generation_id"] == a.generation_id
    result = router.accept_decision(SYMBOL, ready(s, book, b), 11, book)
    assert not result.tradeable
    assert s.frozen is None


def test_r03_matching_level_is_accepted_without_additional_wait():
    a = level()
    rows, book, ctx, structure = market([a])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    d = router.accept_decision(SYMBOL, ready(s, book, a), 10, book)
    assert d.tradeable and s.first_signal_mono == 10


def test_r04_completing_one_object_does_not_consume_an_unowned_level():
    a, b = level(key="A"), level("support", key="B", center=100)
    rows, book, ctx, structure = market([a,b])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert s
    used_level = s.level["generation_id"]
    router.completed(SYMBOL, 11, "target")
    other = router.observe(SYMBOL, replace(ctx, observed_at_ms=ctx.observed_at_ms+1),
                           rows, structure, ENABLED, 12)
    assert other and other.level["generation_id"] != used_level


@pytest.mark.parametrize("direction", [1, -1])
def test_r05_forming_to_closed_does_not_create_another_failed_break(direction):
    obj = level("support" if direction > 0 else "resistance", mature=False, center=100)
    price = 100+direction*.1
    now = 6_010_000
    bar = Candle(6_000_000, 100+direction*.05, 100.15, 99.85, price, 100, 10000, False)
    rows, book, ctx, structure = market([obj], price=price, now=now, forming=bar)
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert s and s.owner == "weak_level_rejection"
    router.completed(SYMBOL, 11, "target")
    assert router.observe(SYMBOL, ctx, rows, structure, ENABLED, 12) is None
    rows = rows + [replace(bar, confirmed=True)]
    new_bar = Candle(6_060_000, price, price+.005, price-.005, price, 5, 500, False)
    now += 60_000
    ctx = replace(ctx, observed_at_ms=now,
                  forming_candle=build_forming_candle_context(new_bar, rows, observed_at_ms=now))
    assert router.observe(SYMBOL, ctx, rows, structure, ENABLED, 70) is None


@pytest.mark.parametrize("direction", [1, -1])
def test_r04_renaming_strategy_on_same_episode_does_not_bypass_consumption(direction):
    obj = level("day_low" if direction > 0 else "day_high", mature=False,
                center=100 if direction > 0 else 100.4)
    rows, book, ctx, structure = market([obj])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert s and s.owner == "level_breakout"
    router.reject(SYMBOL, "risk", "insufficient economics", 11)
    router.completed(SYMBOL, 12, "entry opportunity cancelled")
    # Same object, no new crossing or reclaim. Turning off the winner must not
    # turn the lower-priority reaction into a back door around the risk refusal.
    assert router.observe(SYMBOL, ctx, rows, structure,
                          {"weak_level_rejection": True}, 13) is None


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("next_minute", [False, True])
def test_r05_a_genuine_new_sweep_is_available_but_duplicates_are_not(direction, next_minute):
    obj = level("support" if direction > 0 else "resistance", mature=False, center=100)
    price = 100+direction*.1
    bar = Candle(6_000_000, price, 100.15, 99.85, price, 100, 10000, False)
    rows, book, ctx, structure = market([obj], price=price, forming=bar)
    router = ScenarioRouter()
    first = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    router.completed(SYMBOL, 11, "target")
    before = first.episode_key
    for _ in range(3):
        assert router.observe(SYMBOL, ctx, rows, structure, ENABLED, 12) is None
    elapsed = 60_000 if next_minute else 1_000
    if next_minute:
        rows = rows + [replace(bar, confirmed=True)]
        bar = replace(bar, start_ms=6_060_000, low=price-.005, high=price+.005)
    # Observe a fresh crossing, then its reclaim, rather than inferring a new
    # event merely because an old bar closed.
    crossed = replace(ctx, observed_at_ms=ctx.observed_at_ms+elapsed,
                      last_price=100-direction*.1,
                      forming_candle=build_forming_candle_context(bar, rows,
                          observed_at_ms=ctx.observed_at_ms+elapsed))
    prepared = router.observe(SYMBOL, crossed, rows, structure, ENABLED, 20)
    # Routing before the reclaim is preparation, never an entry signal.
    assert prepared is None or (prepared.first_signal_mono is None and prepared.frozen is None)
    recovered = replace(crossed, observed_at_ms=crossed.observed_at_ms+100, last_price=price)
    second = router.observe(SYMBOL, recovered, rows, structure, ENABLED, 21)
    assert second and second.owner == "weak_level_rejection"
    assert second.episode_key != before
    router.completed(SYMBOL, 22, "target")
    assert router.observe(SYMBOL, recovered, rows, structure, ENABLED, 23) is None


def test_r03_late_fill_restores_exact_object_and_episode():
    from copy import deepcopy
    from types import SimpleNamespace
    a = level()
    rows, book, ctx, structure = market([a])
    router = ScenarioRouter()
    first = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    d = router.accept_decision(SYMBOL, ready(first, book, a), 10, book)
    saved = deepcopy(first.public())
    router.cancelled(SYMBOL, 11, "ack")
    del router.scenarios[SYMBOL]
    position = SimpleNamespace(strategy=first.owner, side=Side(first.side), entry=d.entry,
                               setup_id=d.setup_id, strategy_details={"scenario": saved})
    restored = router.restore_execution(SYMBOL, position, 12)
    assert restored.object_ref == first.object_ref
    assert restored.episode_key == first.episode_key
    assert restored.level == first.level
    router.filled(SYMBOL, 12)
    assert router.observe(SYMBOL, ctx, rows, MarketStructure(), {}, 13, position=position) is restored


def test_r03_removed_object_releases_pre_order_owner():
    a, b = level(key="A"), level(key="B", center=100.5)
    rows, book, ctx, structure = market([a, b])
    router = ScenarioRouter()
    first = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 10)
    assert first.level["generation_id"] == a.generation_id
    remaining = MarketStructure([b])
    assert router.observe(SYMBOL, ctx, rows, remaining, ENABLED, 11) is None
    second = router.observe(SYMBOL, ctx, rows, remaining, ENABLED, 12)
    assert second and second.object_ref != first.object_ref


@pytest.mark.parametrize("owner", ["level_breakout", "weak_level_rejection"])
@pytest.mark.parametrize("direction", [1, -1])
def test_r03_actual_level_strategy_uses_bound_object_not_new_nearer_level(owner, direction):
    from copy import deepcopy
    from scalp_bot.scenario_identity import level_ref
    from scalp_bot.strategy.breakout import LevelBreakoutStrategy
    from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
    from test_breakout_hourly_context import native_inputs, hourly_context
    from test_strategies import weak_support_rejection_candles, rejection_absorption_only_flow
    from test_strategy_level_semantics import young_support
    if owner == "level_breakout":
        rows, structure, ticks = native_inputs(Action.LONG)
        ctx = hourly_context(Action.LONG)
        price, now = 100.165, 30_004_600
        strategy = LevelBreakoutStrategy()
    else:
        rows, structure, ticks = weak_support_rejection_candles(), young_support(), rejection_absorption_only_flow()
        ctx = seed_context()[2]
        price, now = 100.095, 20_002_200
        strategy = WeakLevelRejectionStrategy()
    a = structure.levels[0]
    b = deepcopy(a)
    b.generation_id, b.level_id = "FOREIGN:g1", "FOREIGN"
    # The competing object would normally win the distance-based selector.
    shift = price - b.center
    b.low += shift
    b.high += shift
    structure.levels.append(b)
    if direction < 0:
        rows = [replace(c, open=200-c.open, high=200-c.low, low=200-c.high, close=200-c.close) for c in rows]
        ticks = [replace(t, price=200-t.price, side="Sell" if t.side == "Buy" else "Buy") for t in ticks]
        for obj in structure.levels:
            obj.kind = "support" if obj.kind == "resistance" else "resistance"
            obj.low, obj.high = 200-obj.high, 200-obj.low
        price = 200-price
    ctx = replace(ctx, observed_at_ms=now, last_price=price, forming_candle=None,
                  scenario={"owner": owner, "side": "long" if direction > 0 else "short",
                            "objectRef": level_ref(a).public()})
    d = strategy.evaluate(rows, OrderBook([(price-.005,10000)],[(price+.005,10000)]),
        Trend.UP if direction > 0 else Trend.DOWN, symbol=SYMBOL, trades=ticks,
        structure=structure, market_context=ctx, observed_at_ms=now)
    assert d.details.get("zone", {}).get("low") == pytest.approx(a.low), d.details
    assert d.details.get("zone", {}).get("high") == pytest.approx(a.high), d.details


def test_r03_trendline_identity_is_shared_and_selection_is_bound():
    from scalp_bot.scenario_identity import trendline_ref
    from scalp_bot.strategy.trend_structure import TrendStructureStrategy
    from test_trend_structure import long_pullback_candles, structure as make_structure, buy_flow
    rows, structure = long_pullback_candles(), make_structure("support")
    a = structure.trendlines[0]
    b = replace(a, start_ms=a.start_ms+60_000, score=a.score+10, current_price=a.current_price+.02)
    structure.trendlines.append(b)
    ctx = replace(seed_context()[2], scenario={"owner": "trend_structure", "side": "long",
                                               "objectRef": trendline_ref(a).public()})
    strategy = TrendStructureStrategy()
    d = strategy.evaluate(rows, OrderBook([(100.095,10000)],[(100.105,10000)]), Trend.UP,
        symbol=SYMBOL, trades=buy_flow(), structure=structure, market_context=ctx,
        observed_at_ms=100_500)
    assert d.details["trendline"]["start_ms"] == a.start_ms
    assert trendline_ref(replace(a, end_ms=a.end_ms+60_000, current_price=a.current_price+.01)) == trendline_ref(a)


def test_r03_beta_cannot_replace_assigned_closed_pattern():
    from scalp_bot.scenario_identity import candle_ref
    from scalp_bot.strategy.price_action_hypothesis import PriceActionHypothesisStrategy
    rows, book, ctx, flow = seed_context()
    ctx = replace(ctx, scenario={"owner": "price_action_hypothesis", "side": "long",
                                "objectRef": candle_ref(rows[-2].start_ms).public()})
    d = PriceActionHypothesisStrategy().evaluate(rows, book, Trend.UP, symbol=SYMBOL,
                                                market_context=ctx, trade_flow=flow)
    assert not d.tradeable
    assert "назначенный" in d.reasons[0]


def test_r02_breakout_history_matches_the_playbook_not_router_guess():
    rows, book, ctx, structure = market([level()])
    router = ScenarioRouter()
    assert router.observe(SYMBOL, ctx, rows[-59:], structure, {"level_breakout": True}, 1) is None
    assert router.public(SYMBOL)["situation"]["strategies"]["level_breakout"]["status"] == "insufficient_data"
    assert router.observe(SYMBOL, ctx, rows[-60:], structure, {"level_breakout": True}, 2)


def test_r01_preparation_preview_uses_frozen_runner_policy():
    from scalp_bot.strategy.preparation import preparation_plan
    from scalp_bot.strategy.breakout import LevelBreakoutStrategy
    from scalp_bot.strategy_policy import may_extend_runner
    rows, book, ctx, structure = market([level()])
    s = ScenarioRouter().observe(SYMBOL, ctx, rows, structure, ENABLED, 1)
    wait = StrategyDecision(s.owner, Action.WAIT, [], details={})
    preview = preparation_plan(LevelBreakoutStrategy(), s, wait, book, rows, structure)
    assert preview and not may_extend_runner(preview.details)


def test_r03_repeated_decision_cannot_change_liquidity_target_metadata():
    a = level()
    rows, book, ctx, structure = market([a])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 1)
    first = ready(s, book, a)
    first.details["liquidityTarget"] = {"price": first.target}
    router.accept_decision(SYMBOL, first, 2, book)
    second = ready(s, book, a)
    second.details["liquidityTarget"] = {"price": first.target+10}
    second.target += 10
    actual = router.accept_decision(SYMBOL, second, 3, book)
    assert actual.tradeable
    assert actual.details["liquidityTarget"]["price"] == actual.target == first.target


def test_r03_repeat_cannot_invent_previously_absent_liquidity_target():
    a = level()
    rows, book, ctx, structure = market([a])
    router = ScenarioRouter()
    s = router.observe(SYMBOL, ctx, rows, structure, ENABLED, 1)
    first = ready(s, book, a)
    router.accept_decision(SYMBOL, first, 2, book)
    second = ready(s, book, a)
    second.details["liquidityTarget"] = {"price": first.target+10}
    accepted = router.accept_decision(SYMBOL, second, 3, book)
    assert accepted.tradeable and "liquidityTarget" not in accepted.details
