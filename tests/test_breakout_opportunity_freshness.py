"""Price-episode freshness must survive confirmation waits and price gaps."""
from dataclasses import replace

import pytest

from scalp_bot.domain import Action, OrderBook, Trend
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.semantic_arbiter import assess_candidate
from test_entry_freshness import make_engine, close_engine, tradeable
from scalp_bot.engine import ActiveSymbolSession
from test_strategies import mature_breakout_candles


@pytest.mark.parametrize("short", [False, True])
def test_price_episode_precedes_flow_and_resets_only_on_price_or_generation(monkeypatch, short):
    strategy = LevelBreakoutStrategy()
    rows = mature_breakout_candles()
    if short:
        rows = [replace(c, open=200-c.open, high=200-c.low,
                        low=200-c.high, close=200-c.close) for c in rows]
    def evaluate(mid, now):
        return strategy.evaluate(rows, OrderBook(bids=[(mid-.005, 50)], asks=[(mid+.005, 50)]),
            Trend.DOWN if short else Trend.UP, symbol="AAA", trades=[], observed_at_ms=now)
    outside = 99.835 if short else 100.165
    first = evaluate(outside, 100_000)
    assert first.action == Action.WAIT  # no executions to confirm the break
    trigger = first.details["opportunityTrigger"]
    zone = first.details["zone"]
    assert trigger["price"] == zone["low" if short else "high"]
    assert trigger["price"] != outside  # a gap does not reset spent move to zero
    assert trigger["observedAtMs"] == 100_000
    # Volatility and elapsed time grow while flow is missing, but the original
    # opportunity does not get a larger budget or a younger timestamp.
    monkeypatch.setattr("scalp_bot.strategy.breakout.typical_range_abs", lambda _: 5.0)
    repeated = evaluate(outside, 500_000)
    assert repeated.details["opportunityTrigger"] == trigger
    near = trigger["price"] * (1 - .0003 if short else 1 + .0003)
    assert evaluate(near, 501_000).details["opportunityTrigger"] == trigger
    inside = (zone["low"] + zone["high"]) / 2
    assert evaluate(inside, 502_000).details["opportunityTrigger"] is None
    fresh = evaluate(outside, 503_000).details["opportunityTrigger"]
    assert fresh["observedAtMs"] == 503_000
    assert fresh["expectedImpulsePct"] > trigger["expectedImpulsePct"]
    monkeypatch.setattr(strategy, "_generation", lambda _: ("support" if short else "resistance", "new"))
    changed = evaluate(outside, 504_000).details["opportunityTrigger"]
    assert changed["observedAtMs"] == 504_000
    assert changed["generation"] != fresh["generation"]


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("move,classification,allowed", [(.1, "fresh", True), (.6, "late", True), (.9, "exhausted", False)])
def test_fire_does_not_erase_spent_move_but_waiting_does_not_spend_price_budget(tmp_path, action, move, classification, allowed):
    engine = make_engine(tmp_path)
    session = ActiveSymbolSession(symbol="AAA")
    sign = 1 if action == Action.LONG else -1
    entry = 100 + sign * move
    decision = tradeable("level_breakout", action=action, entry=entry,
        stop=entry-sign, target=entry+2*sign, state="impulse", details={
            "expectedImpulsePct": .10,  # a later estimate cannot enlarge the episode budget
            "opportunityArm": {"observedAtMs": 1_000, "price": 100.0},
            "fireTrigger": {"observedAtMs": 500_000, "price": entry},
            "opportunityTrigger": {"observedAtMs": 100_000, "price": 100.0,
                "expectedImpulsePct": .01, "source": "breakout_price_episode"},
        })
    try:
        engine._annotate_entry_freshness(session, decision, observed_at=500.02)
        execution = decision.details["entryFreshness"]
        opportunity = decision.details["opportunityFreshness"]
        assert execution["classification"] == "fresh"
        assert execution["confirmationAgeSeconds"] == pytest.approx(.02)
        assert opportunity["confirmationAgeSeconds"] == pytest.approx(400.02)
        assert opportunity["timeSpentRatio"] is None
        assert opportunity["moveSpentRatio"] == pytest.approx(move)
        assert opportunity["classification"] == classification
        assessment = assess_candidate(decision, None)
        assert assessment.allowed is allowed
        if not allowed:
            assert "opportunity_exhausted" in assessment.blockers
    finally:
        close_engine(engine)


def test_stale_fire_still_blocks_when_price_opportunity_is_fresh(tmp_path):
    engine = make_engine(tmp_path)
    session = ActiveSymbolSession(symbol="AAA")
    decision = tradeable("level_breakout", action=Action.LONG, entry=100.1,
        stop=99, target=102, state="impulse", details={
            "fireTrigger": {"observedAtMs": 100_000, "price": 100.1},
            "opportunityTrigger": {"observedAtMs": 90_000, "price": 100.0,
                "expectedImpulsePct": .01, "source": "breakout_price_episode"},
        })
    try:
        engine._annotate_entry_freshness(session, decision, observed_at=300)
        assert decision.details["opportunityFreshness"]["classification"] == "fresh"
        assert decision.details["entryFreshness"]["classification"] == "exhausted"
        assert "entry_signal_exhausted" in assess_candidate(decision, None).blockers
    finally:
        close_engine(engine)
