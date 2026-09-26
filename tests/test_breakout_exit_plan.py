import json
from pathlib import Path

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, Side, StrategyDecision
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.paper import PaperBroker
from scalp_bot.risk import RiskEngine
from scalp_bot.runtime_clock import ReplayRuntimeClock
from scalp_bot.strategy.semantic_arbiter import assess_structural_path
from scalp_bot.strategy_policy import breakout_impulse_limit
from test_paper import book, plan
from test_semantic_arbiter import context, mature_level


def settings(**changes):
    return Settings(_env_file=None, **{
        "exchange_clock_enabled": True, "taker_fee_rate": .00055,
        "maker_fee_rate": .0002, "slippage_bps": 0,
        "enforce_min_net_profit_gate": False, "enforce_net_reward_risk_gate": False,
        "absolute_min_net_reward_risk": 0, "max_leverage": 2,
        "max_position_leverage": 2, "max_total_risk_fraction": .1,
        "max_entry_drift_bps": 20, "maker_fill_confirmation_bps": 0,
        **changes,
    })


def build(side, *, cfg=None, details=None, entry=100, fill=100, instrument=None):
    sign = 1 if side == Side.LONG else -1
    d = StrategyDecision(strategy="level_breakout", action=Action(side.value), reasons=["test"],
        entry=entry, stop=entry-sign, target=entry+2*sign,
        details={"allowRunner": True, "expectedImpulsePct": .0025, **(details or {})})
    market = book(fill-.01, fill) if side == Side.LONG else book(fill, fill+.01)
    result = RiskEngine(cfg or settings()).build_plan("AAA", d, 1000, market, 2000, 100, instrument=instrument)
    return d, market, result


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_plan_caps_first_take_and_maker_fills_only_on_confirmed_tape(side):
    cfg = settings()
    d, market, result = build(side, cfg=cfg)
    assert result.allowed, result.reason
    p = result.plan
    e = p.strategy_details["economics"]
    assert e["partialCappedByImpulse"] and e["partialPlanned"]
    assert e["partialMovePct"] == pytest.approx(.0025)
    sign = 1 if side == Side.LONG else -1
    limit = 100 + sign * .25
    assert e["partialPrice"] == pytest.approx(limit)
    clock = ReplayRuntimeClock(wall_seconds=100, mono_ns=100*10**9)
    broker = PaperBroker(cfg, clock=clock)
    pos = broker.open(p, market)
    # An increased volatility estimate after entry cannot move the planned take.
    pos.strategy_details["expectedImpulsePct"] = .1
    assert broker._partial_limit_price(pos) == pytest.approx(limit)
    exit_book = book(limit, limit+.01) if side == Side.LONG else book(limit-.01, limit)
    assert broker.mark("AAA", limit, exit_book, trade_price=None) == []
    required = broker._maker_required_trade_notional(broker._partial_close_quantity(pos)*limit)
    assert broker.mark("AAA", limit, exit_book, trade_price=limit,
                       trade_notional_usd=required, trade_side="Sell" if side == Side.LONG else "Buy") == []
    assert broker.mark("AAA", limit, exit_book, trade_price=limit,
                       trade_notional_usd=required*.5, trade_side="Buy" if side == Side.LONG else "Sell") == []
    events = broker.mark("AAA", limit, exit_book, trade_price=limit,
                         trade_notional_usd=required*.5, trade_side="Buy" if side == Side.LONG else "Sell")
    partial = next(r for r in events if r["event"] == "partial_take")
    assert partial["fill"] == pytest.approx(limit)
    assert partial["netPnl"] == pytest.approx(e["partialNetAtTriggerUsd"])
    assert partial["closedQuantity"] == pytest.approx(p.quantity*.3)
    assert pos.partial_taken
    # Cost-aware protection is still applied to the remaining position.
    closed = broker.mark("AAA", pos.stop, book(pos.stop, pos.stop), trade_price=None)[-1]
    assert closed["reason"] == "stop"
    assert closed["netPnl"] >= partial["netPnl"] - .01


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_price_episode_and_fill_drift_cannot_renew_impulse_budget(side):
    sign = 1 if side == Side.LONG else -1
    trigger = {"price": 100, "expectedImpulsePct": .0025}
    _, _, result = build(side, entry=100+sign*.1, fill=100+sign*.15,
                         details={"opportunityTrigger": trigger, "expectedImpulsePct": .1})
    assert result.allowed, result.reason
    e = result.plan.strategy_details["economics"]
    assert e["partialPrice"] == pytest.approx(100+sign*.25)
    assert e["impulseFirstTakeLimitSource"] == "price_episode"
    assert e["partialMovePct"] < .00101


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("case", ["fees", "profit_floor", "spent", "rounding"])
def test_unusable_first_take_cannot_fall_back_to_distant_target(side, case):
    cfg, details, instrument = settings(), {"expectedImpulsePct": .0001}, None
    if case == "profit_floor":
        cfg, details = settings(enforce_min_net_profit_gate=True, min_net_profit_usd=10), {"expectedImpulsePct": .0025}
    elif case == "spent":
        details = {"opportunityTrigger": {"price": 99 if side == Side.LONG else 101, "expectedImpulsePct": .001}}
    elif case == "rounding":
        cfg = settings(taker_fee_rate=0, maker_fee_rate=0)
        instrument = InstrumentSpec.from_bybit({"symbol": "AAA", "status": "Trading", "priceFilter": {"tickSize": "0.1"}})
    _, _, result = build(side, cfg=cfg, details=details, instrument=instrument)
    assert not result.allowed
    assert "impulse first take" in result.reason
    assert result.diagnostics["partialCappedByImpulse"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_structural_path_uses_same_capped_and_planned_absolute_price(side):
    d, _, result = build(side)
    sign = 1 if side == Side.LONG else -1
    obstacle = mature_level("resistance" if sign == 1 else "support",
        100.4 if sign == 1 else 99.5, 100.5 if sign == 1 else 99.6, generation="foreign")
    ctx = context(resistance=obstacle) if sign == 1 else context(support=obstacle)
    before = assess_structural_path(d, ctx)
    assert before.first_take_price == pytest.approx(100+sign*.25)
    assert not before.obstacle_before_first_take
    d.details.update(plannedPartialEnabled=True, plannedFirstTakePrice=100+sign*.2)
    after = assess_structural_path(d, ctx)
    assert after.first_take_price == pytest.approx(100+sign*.2)
    assert not after.obstacle_before_first_take


def test_other_strategies_and_missing_estimate_keep_r_multiple():
    for strategy in ("weak_level_rejection", "trend_structure", "price_action_hypothesis"):
        assert breakout_impulse_limit(strategy, Side.LONG, 100, {"expectedImpulsePct": .0025}) == (None, None)
    _, _, result = build(Side.LONG, details={"expectedImpulsePct": None})
    assert result.allowed
    assert result.plan.strategy_details["economics"]["partialPrice"] == pytest.approx(101)
    assert not result.plan.strategy_details["economics"]["partialCappedByImpulse"]


def test_market_partial_does_not_fire_on_historical_mfe_after_quote_retreat():
    cfg = settings()
    broker = PaperBroker(cfg)
    p = plan("AAA", Side.LONG, 400)
    p.strategy_details = {"economics": {"partialPrice": 100.25, "partialPlanned": True}}
    pos = broker.open(p, book(99.99, 100))
    pos.mfe_usd = pos.initial_risk_usd * 2
    assert not broker._partial_triggered(pos, 100.1, "taker_market")
    assert broker._partial_triggered(pos, 100.25, "taker_market")


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_breakout_loses_old_mfe_immunity_but_recent_progress_keeps_position(side):
    cfg = settings(partial_take_enabled=False)
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=100*10**9)
    broker = PaperBroker(cfg, clock=clock)
    p = plan("AAA", side, 400)
    p.strategy = "level_breakout"
    pos = broker.open(p, book(99.99, 100) if side == Side.LONG else book(100, 100.01))
    sign = 1 if side == Side.LONG else -1
    risk = abs(pos.entry-pos.initial_stop)
    def mark(mono, r, wall):
        clock.set_observation(wall_seconds=wall, mono_ns=mono*10**9)
        price = pos.entry+sign*risk*r
        return broker.mark("AAA", price, book(price, price), trade_price=None)
    assert mark(110, .8, 1010) == []
    assert pos.mfe_r > cfg.no_follow_through_max_mfe_r
    # Wall clock jumps must not expire the 120-second monotonic timeout.
    assert mark(220, .3, 999999) == []  # only 110 s since peak
    assert mark(230, .85, 900) == []   # new high starts a fresh progress interval
    assert mark(349, .3, 901) == []
    events = mark(350, .3, 902)
    closed = events[-1]
    assert closed["reason"] == "no_follow_through"
    assert closed["netPnl"] > 0  # price can still be positive when continuation fails
    detail = pos.strategy_details["noFollowThroughExit"]
    assert detail["cause"] == "stalled_giveback"
    assert detail["sincePeakSeconds"] == 120
    assert detail["givebackR"] == pytest.approx(.55)


def test_other_strategy_keeps_old_no_follow_through_policy():
    cfg = settings(partial_take_enabled=False)
    clock = ReplayRuntimeClock(wall_seconds=100, mono_ns=100*10**9)
    broker = PaperBroker(cfg, clock=clock)
    pos = broker.open(plan("AAA", Side.LONG), book(99.99, 100))
    pos.mfe_usd = pos.initial_risk_usd*.8
    clock.set_observation(wall_seconds=500, mono_ns=500*10**9)
    assert not broker._should_cut_no_follow_through(pos, -pos.initial_risk_usd*.5)


def test_recorded_xrp_first_take_geometry_includes_fill_costs_and_tick_rounding():
    f = json.loads((Path(__file__).parent / "fixtures/xrp_first_take.json").read_text(encoding="utf-8"))
    cfg = settings(taker_fee_rate=f["entryFeeRate"], maker_fee_rate=f["partialFeeRate"],
                   slippage_bps=f["entrySlippageBps"], breakout_partial_take_fraction=f["partialFraction"])
    d = StrategyDecision(strategy="level_breakout", action=Action.LONG, reasons=["recorded XRP geometry"],
        entry=f["setupEntry"], stop=f["stop"], target=f["target"],
        details={"expectedImpulsePct": f["expectedImpulsePct"], "allowRunner": True})
    instrument = InstrumentSpec.from_bybit({"symbol": "XRPUSDT", "status": "Trading",
        "priceFilter": {"tickSize": "0.0001"}, "lotSizeFilter": {"qtyStep": "0.1"}})
    market = OrderBook(bids=[(f["bid"], f["bidSize"])], asks=[(f["ask"], f["askSize"])])
    # The notional cap reuses the recorded exposure; this is not a new entry decision.
    result = RiskEngine(cfg).build_plan("XRPUSDT", d, 1000, market, f["notional"], 100, instrument=instrument)
    assert result.allowed, result.reason
    e = result.plan.strategy_details["economics"]
    assert result.plan.market_entry == pytest.approx(f["fill"])
    assert f["oldFirstTakeMovePct"] > f["expectedImpulsePct"]
    assert e["partialPrice"] == pytest.approx(1.5487)
    assert e["firstTakeMovePct"] == pytest.approx(.002554154139711679)
    assert e["firstTakeMovePct"] < f["expectedImpulsePct"]
    assert e["partialNetAtTriggerUsd"] > 0


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_profitable_full_target_inside_impulse_does_not_require_partial(side):
    d, market, _ = build(side)
    sign = 1 if side == Side.LONG else -1
    d.target = 100 + sign * .2
    result = RiskEngine(settings()).build_plan("AAA", d, 1000, market, 2000, 100)
    assert result.allowed, result.reason
    e = result.plan.strategy_details["economics"]
    assert not e["partialPlanned"]
    assert e["firstTakePrice"] == pytest.approx(d.target)
    assert e["firstTakeMovePct"] <= d.details["expectedImpulsePct"]


def test_planned_path_uses_fill_when_price_is_better_than_setup():
    d, _, _ = build(Side.LONG)
    d.details.update(plannedEntryPrice=99.8, plannedFirstTakePrice=99.9, plannedPartialEnabled=True)
    path = assess_structural_path(d, context())
    assert path.first_take_price == pytest.approx(99.9)
    assert path.first_take_distance_pct == pytest.approx(.1/99.8)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_unprofitable_small_r_cannot_disable_partial_and_assume_far_runner(side):
    d, market, _ = build(side, details={"expectedImpulsePct": .001})
    sign = 1 if side == Side.LONG else -1
    d.stop = d.entry - sign * .04  # 4 bps at 1R cannot cover the partial's 7.5 bps cost
    result = RiskEngine(settings()).build_plan("AAA", d, 1000, market, 2000, 100)
    assert not result.allowed
    assert "impulse first take" in result.reason
    assert not result.diagnostics["partialCappedByImpulse"]
    assert not result.diagnostics["partialEconomicReady"]
    # Closing the whole position at a nearer profitable target remains valid.
    d.target = d.entry + sign * .09
    closer = RiskEngine(settings()).build_plan("AAA", d, 1000, market, 2000, 100)
    assert closer.allowed, closer.reason
    assert not closer.plan.strategy_details["economics"]["partialPlanned"]
    assert closer.plan.strategy_details["economics"]["firstTakePrice"] == pytest.approx(d.target)
