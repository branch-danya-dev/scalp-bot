"""Current breakout response replaces the rolling response veto, symmetrically."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from breakout_fixtures import breakout_executions
from scalp_bot.domain import Action, OrderBook, TradeTick, Trend
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.flow import flow_beyond_level
from test_breakout_hourly_context import native_inputs


@pytest.mark.parametrize("short", [False, True])
def test_flow_is_sorted_clipped_to_episode_boundary_and_observation(short):
    prices = [100.10, 99.99, 100.12, 100.15, 100.18, 100.30]
    ticks = [TradeTick(ts, 200-p if short else p, 1, "Sell" if short else "Buy")
             for ts, p in zip((6_000, 7_000, 8_000, 9_000, 10_000, 11_000), prices)]
    result = flow_beyond_level(list(reversed(ticks)), 100, long_side=not short,
                              now_ms=10_000, since_ms=7_000)
    assert result.trade_count == 3
    assert result.first_price == pytest.approx(99.88 if short else 100.12)
    assert result.last_price == pytest.approx(99.82 if short else 100.18)
    assert result.imbalance == (-1 if short else 1)
    recent = flow_beyond_level(ticks, 100, long_side=not short,
                              now_ms=10_000, since_ms=1_000, seconds=1)
    assert recent.trade_count == 2  # the rolling bound remains in force
    assert recent.first_price == pytest.approx(99.85 if short else 100.15)
    assert flow_beyond_level(ticks, 100, long_side=not short,
                            now_ms=10_000, since_ms=10_001).trade_count == 0


def scenario(short=False, *, old_price=100.16, routed=False):
    action = Action.SHORT if short else Action.LONG
    rows, structure, ticks = native_inputs(action)
    # These are pre-break executions; their rolling response must not FIRE.
    ticks = [replace(t, price=200-old_price if short else old_price) for t in ticks]
    strategy = LevelBreakoutStrategy()
    strategy.staged_entries_enabled = False

    def evaluate(now=30_014_000, mid=100.165, extra=()):
        price = 200-mid if short else mid
        from test_price_action_hypothesis import scenario as beta_context
        context = beta_context(-1 if short else 1)[2] if routed else None
        if context:
            context = replace(context, observed_at_ms=now,
                scenario={"owner":"level_breakout", "side":"short" if short else "long"})
        return strategy.evaluate(
            rows, OrderBook(bids=[(price-.005, 50)], asks=[(price+.005, 50)]),
            Trend.DOWN if short else Trend.UP, symbol="CURRENT", trades=ticks+list(extra),
            structure=structure, observed_at_ms=now, market_context=context,
        )

    first = evaluate(30_004_600)
    assert first.action == Action.WAIT
    assert first.details["breakHoldSeconds"] == 0
    assert first.details["acceptanceFlow"]["tradeCount"] <= 1
    return strategy, evaluate, first


@pytest.mark.parametrize("short", [False, True])
def test_current_response_confirms_while_rolling_response_still_waits(short):
    strategy, evaluate, first = scenario(short)
    fired = evaluate(extra=breakout_executions(short=short))
    assert fired.action == (Action.SHORT if short else Action.LONG)
    assert fired.details["confirmationRule"] == "current_acceptance_v1"
    assert fired.details["rollingLevelResponseBps"] < 1
    assert fired.details["currentAcceptanceResponseBps"] > 4
    assert fired.details["currentQuoteResponseBps"] > 3
    assert fired.details["sustainedResponseReady"] is True
    assert fired.details["breakHoldSeconds"] == pytest.approx(9.4)
    assert fired.details["opportunityTrigger"] == first.details["opportunityTrigger"]
    assert strategy.hold_without_retest_seconds == 8


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("case", ["flat_tape", "quote_retreated", "no_current_trades", "only_two_trades", "opposed_flow"])
def test_old_response_or_quote_alone_cannot_confirm(short, case):
    strategy, evaluate, _ = scenario(short, old_price=100.08)
    ticks = breakout_executions(last=100.155, short=short)
    mid = 100.165
    if case == "flat_tape":
        ticks = breakout_executions(first=100.155, last=100.155, short=short)
    elif case == "quote_retreated":
        mid = 100.14  # tape advanced, but executable quote has already returned
    elif case == "no_current_trades":
        ticks = []
    elif case == "only_two_trades":
        ticks = [ticks[0], ticks[-1]]
    elif case == "opposed_flow":
        ticks = [replace(t, side="Buy" if short else "Sell") for t in ticks]
    decision = evaluate(mid=mid, extra=ticks)
    assert decision.action == Action.WAIT
    assert decision.details["breakHoldSeconds"] >= strategy.hold_without_retest_seconds
    if case in ("flat_tape", "quote_retreated", "only_two_trades", "opposed_flow"):
        assert decision.details["rollingLevelResponseBps"] > 5
    if case == "quote_retreated":
        assert decision.details["currentAcceptanceResponseBps"] > 3
        assert decision.details["currentQuoteResponseBps"] < 2


@pytest.mark.parametrize("short", [False, True])
def test_current_response_does_not_shorten_hold_and_price_return_restarts_it(short):
    _, evaluate, first = scenario(short)
    ticks = breakout_executions(30_010_000, short=short)
    too_soon = evaluate(30_010_000, extra=ticks)
    assert too_soon.action == Action.WAIT
    assert too_soon.details["sustainedResponseReady"] is True
    assert too_soon.details["breakHoldSeconds"] == pytest.approx(5.4)
    # Back in the zone: neither elapsed time nor previous executions transfer.
    inside = evaluate(30_010_100, mid=100.015, extra=ticks)
    assert inside.details["opportunityTrigger"] is None
    returned = evaluate(30_014_000, extra=breakout_executions(short=short))
    assert returned.action == Action.WAIT
    assert returned.details["breakHoldSeconds"] == 0
    assert returned.details["opportunityTrigger"]["observedAtMs"] > first.details["opportunityTrigger"]["observedAtMs"]
    assert returned.details["acceptanceFlow"]["tradeCount"] == 1


def test_recorded_xrp_windows_distinguish_current_impulse_from_later_flat_price():
    fixture = json.loads((Path(__file__).parent / "fixtures/xrp_current_response.json").read_text(encoding="utf-8"))
    for sample in fixture["samples"]:
        ticks = [TradeTick(t["ts"], t["price"], t["size"], t["side"], t["sequence"]) for t in sample["trades"]]
        result = flow_beyond_level(ticks, sample["boundary"], long_side=True,
                                  now_ms=sample["observedAtMs"])
        assert result.trade_count == sample["recordedTradeCount5s"]
        assert result.total_notional == pytest.approx(sample["recordedNotional5s"])
        assert result.price_response_pct * 10_000 == pytest.approx(sample["expectedTapeBps"])
        quote_bps = (sample["bid"] - result.first_price) / result.first_price * 10_000
        assert quote_bps == pytest.approx(sample["expectedQuoteBps"])
    # This checks real inputs to the response rule, not an alternative trade/PnL.


@pytest.mark.parametrize("short", [False, True])
def test_routed_breakout_fires_on_causal_response_without_extra_hold(short):
    _, evaluate, first = scenario(short, routed=True)
    ticks = breakout_executions(30_010_000, short=short)
    fired = evaluate(30_010_000, extra=ticks)
    assert fired.action == (Action.SHORT if short else Action.LONG)
    assert fired.details["confirmationRule"] == "causal_acceptance_v2"
    assert fired.details["breakHoldSeconds"] == pytest.approx(5.4)
    assert fired.details["sustainedResponseReady"]
    assert fired.details["opportunityTrigger"] == first.details["opportunityTrigger"]


@pytest.mark.parametrize("short", [False, True])
def test_routed_breakout_does_not_replace_response_with_elapsed_time(short):
    _, evaluate, _ = scenario(short, routed=True)
    flat = breakout_executions(first=100.155, last=100.155, short=short)
    assert evaluate(extra=flat).action == Action.WAIT
