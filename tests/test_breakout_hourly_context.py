"""Hourly-only opposition must not veto a proven local breakout unconditionally."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from scalp_bot.domain import Action, OrderBook, Trend
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.flow_context import build_multi_horizon_flow_context
from scalp_bot.strategy.market_context import ExecutionContext
from scalp_bot.strategy.playbook_context import PlaybookKind, assess_entry_context
from scalp_bot.strategy.regime import HTFBias, HTFBiasSnapshot, LocalRegime, LocalRegimeSnapshot
from scalp_bot.strategy.semantic_arbiter import assess_candidate
from test_playbook_context import context
from test_strategy_level_semantics import (
    aggressive_buy_flow,
    mature_breakout_candles,
    mature_structure,
)


def hourly_context(action, *, scores=(0.7, 0.6, 0.5)):
    long = action == Action.LONG
    direction, opposite = (Trend.UP, Trend.DOWN) if long else (Trend.DOWN, Trend.UP)
    trade_flow = {}
    for seconds, score in zip((5, 15, 60), scores):
        trade_flow.update({
            f"imbalance{seconds}s": (score or 0.0) * (1 if long else -1),
            f"notional{seconds}s": seconds * 10_000,
            f"tradeCount{seconds}s": seconds * 10 if score is not None else 0,
        })
    ctx = context(
        LocalRegime.BULLISH_IMPULSE if long else LocalRegime.BEARISH_IMPULSE,
        direction=direction,
        parent=Trend.FLAT,
        flow=build_multi_horizon_flow_context(trade_flow, {}, observed_at_ms=30_004_600),
    )
    return replace(ctx, htf_bias=HTFBiasSnapshot(
        bias=HTFBias.BEARISH if long else HTFBias.BULLISH,
        strength=0.6,
        trend_15m=Trend.FLAT,
        trend_1h=opposite,
        alignment="1h_only",
        legacy_trend=Trend.FLAT,
    ))


def assess(ctx, action, *, confirmed=True):
    return assess_entry_context(
        PlaybookKind.LEVEL_BREAKOUT, action, ctx, Trend.FLAT,
        breakout_confirmed=confirmed,
    )


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("trend_regime", [False, True])
def test_confirmed_local_breakout_can_override_hourly_only(action, trend_regime):
    ctx = hourly_context(action)
    if trend_regime:
        ctx = replace(ctx, local_regime=replace(
            ctx.local_regime,
            regime=LocalRegime.BULLISH_TREND if action == Action.LONG else LocalRegime.BEARISH_TREND,
        ))
    result = assess(ctx, action)
    assert result.allowed
    assert result.blockers == ()
    assert result.htf_override == "confirmed_local_breakout"
    assert result.public()["htfOverride"] == "confirmed_local_breakout"
    # The opposing hourly context remains visible; it is not relabelled neutral.
    assert result.direction_plan.htf_bias == ctx.htf_bias.bias.value
    assert result.flow_classification == "strongly_aligned"


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
def test_confirmation_is_required_and_defaults_to_blocking(action):
    ctx = hourly_context(action)
    result = assess_entry_context(PlaybookKind.LEVEL_BREAKOUT, action, ctx, Trend.FLAT)
    assert not result.allowed
    assert result.blockers == ("breakout_htf_opposed",)
    assert result.htf_override is None


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("alignment", ["aligned", "15m_only", "unknown", "inconsistent_1h_only"])
def test_opposing_15m_or_unknown_htf_cannot_be_overridden(action, alignment):
    ctx = hourly_context(action)
    ctx = replace(ctx, htf_bias=replace(
        ctx.htf_bias,
        alignment="1h_only" if alignment == "inconsistent_1h_only" else alignment,
        trend_15m=ctx.htf_bias.trend_1h,
        trend_1h=Trend.FLAT if alignment == "15m_only" else ctx.htf_bias.trend_1h,
    ))
    result = assess(ctx, action)
    assert not result.allowed
    assert result.blockers == ("breakout_htf_opposed",)
    assert result.htf_override is None


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("scores", [
    (0.7, 0.6, 0.0),  # Aligned is insufficient: all three horizons must agree.
    (0.7, -0.6, -0.5),  # A short-term reversal is not a confirmed local trend.
    (-0.7, -0.6, -0.5),
    (0.0, 0.0, 0.0),
    (None, None, None),
    None,
])
def test_weak_opposed_or_missing_flow_keeps_hourly_veto(action, scores):
    ctx = hourly_context(action, scores=scores or (0.0, 0.0, 0.0))
    if scores is None:
        ctx = replace(ctx, flow=None)
    result = assess(ctx, action)
    assert not result.allowed
    assert "breakout_htf_opposed" in result.blockers
    assert result.htf_override is None


@pytest.mark.parametrize("regime", [
    LocalRegime.RANGE, LocalRegime.UNCLEAR, LocalRegime.PULLBACK,
    LocalRegime.TRANSITION, LocalRegime.BULLISH_IMPULSE, None,
])
def test_missing_unclear_or_opposing_local_regime_keeps_hourly_veto(regime):
    ctx = hourly_context(Action.SHORT)
    local = replace(ctx.local_regime, regime=regime) if regime is not None else None
    result = assess(replace(ctx, local_regime=local), Action.SHORT)
    assert not result.allowed
    assert "breakout_htf_opposed" in result.blockers


@pytest.mark.parametrize("field", ["book_fresh", "candle_fresh"])
def test_hourly_override_does_not_bypass_execution_readiness(field):
    ctx = hourly_context(Action.SHORT)
    ctx = replace(ctx, execution=replace(ctx.execution, **{field: False}))
    result = assess(ctx, Action.SHORT)
    assert not result.allowed
    assert result.blockers == ("execution_context_not_ready",)


@pytest.mark.parametrize("playbook", [PlaybookKind.LEVEL_REJECTION, PlaybookKind.TREND_CONTINUATION])
def test_other_playbooks_are_unchanged(playbook):
    ctx = hourly_context(Action.SHORT)
    old = assess_entry_context(playbook, Action.SHORT, ctx, Trend.FLAT)
    new = assess_entry_context(playbook, Action.SHORT, ctx, Trend.FLAT, breakout_confirmed=True)
    assert old == new
    assert new.htf_override is None


def native_inputs(action):
    rows, structure, ticks = mature_breakout_candles(), mature_structure(), aggressive_buy_flow()
    if action == Action.SHORT:
        for c in rows:
            c.open, c.high, c.low, c.close = 200-c.open, 200-c.low, 200-c.high, 200-c.close
        level = structure.levels[0]
        level.kind = "support"
        level.low, level.high = 200-level.high, 200-level.low
        level.level_id, level.generation_id = "S:100", "S:100:g1"
        for tick in ticks:
            tick.price = 200-tick.price
            tick.side = "Sell" if tick.side == "Buy" else "Buy"
    return rows, structure, ticks


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("mode", ["sustained_price_response", "retest_response"])
def test_native_breakout_waits_then_confirms_against_hourly_only(action, mode):
    strategy = LevelBreakoutStrategy()
    rows, structure, ticks = native_inputs(action)
    ctx = hourly_context(action)

    def evaluate(mid):
        price = 200-mid if action == Action.SHORT else mid
        return strategy.evaluate(
            rows, OrderBook(bids=[(price-.005, 50)], asks=[(price+.005, 50)]),
            Trend.FLAT, symbol="HOURLYUSDT", trades=ticks,
            structure=structure, market_context=ctx,
        )

    first = evaluate(100.165)
    assert first.action == Action.WAIT
    assert first.details["breakoutConfirmationMode"] is None
    state = strategy._states["HOURLYUSDT"]
    if mode == "retest_response":
        assert evaluate(100.070).action == Action.WAIT
        assert state.retest_seen
        state.retest_at -= 4
        assert evaluate(100.070).action == Action.WAIT  # Time alone is insufficient.
        entry_price = 100.095
    else:
        state.break_started_at -= strategy.hold_without_retest_seconds + 1
        entry_price = 100.165

    fired = evaluate(entry_price)
    assert fired.action == action
    assert fired.details["breakoutConfirmationMode"] == mode
    assert fired.details["entryContextAssessment"]["htfOverride"] == "confirmed_local_breakout"
    assert fired.details["stagedEntry"]["confirmationReady"]
    assert fired.stop < fired.entry < fired.target if action == Action.LONG else fired.target < fired.entry < fired.stop
    assert abs(fired.target-fired.entry) / abs(fired.entry-fired.stop) >= 2
    # The downstream arbiter consumes the same assessment; no second HTF veto.
    semantic = assess_candidate(fired, ctx, partial_take_at_r=1, partial_take_enabled=True)
    assert semantic.allowed
    assert semantic.risk_scale <= 1

    # A subsequent evaluation must recheck the context, not retain the exception.
    ctx = replace(ctx, htf_bias=replace(ctx.htf_bias, alignment="aligned", trend_15m=ctx.htf_bias.trend_1h))
    blocked = evaluate(entry_price)
    assert blocked.action == Action.WAIT
    assert "breakout_htf_opposed" in blocked.details["entryContextAssessment"]["blockers"]


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
def test_native_early_probe_cannot_bypass_hourly_veto(action):
    strategy = LevelBreakoutStrategy()
    strategy.staged_entries_enabled = True
    rows, structure, ticks = native_inputs(action)
    price = 100.165 if action == Action.LONG else 99.835
    decision = strategy.evaluate(
        rows, OrderBook(bids=[(price-.005, 50)], asks=[(price+.005, 50)]),
        Trend.FLAT, symbol="PROBEUSDT", trades=ticks,
        structure=structure, market_context=hourly_context(action),
    )
    assert decision.action == Action.WAIT
    assert decision.details["breakoutConfirmed"] is False
    assert decision.details["entryContextAssessment"]["blockers"] == ["breakout_htf_opposed"]


@pytest.mark.parametrize("action", [Action.LONG, Action.SHORT])
def test_hourly_override_keeps_stop_distance_limit(action):
    strategy = LevelBreakoutStrategy()
    strategy.max_stop_pct = 0.001
    rows, structure, ticks = native_inputs(action)
    price = 100.165 if action == Action.LONG else 99.835
    book = OrderBook(bids=[(price-.005, 50)], asks=[(price+.005, 50)])
    kwargs = dict(symbol="STOPUSDT", trades=ticks, structure=structure, market_context=hourly_context(action))
    assert strategy.evaluate(rows, book, Trend.FLAT, **kwargs).action == Action.WAIT
    strategy._states["STOPUSDT"].break_started_at -= strategy.hold_without_retest_seconds + 1
    blocked = strategy.evaluate(rows, book, Trend.FLAT, **kwargs)
    assert blocked.action == Action.WAIT
    assert blocked.details["stopDistancePct"] > strategy.max_stop_pct


def test_recorded_eth_context_no_longer_has_unconditional_hourly_veto():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "eth_hourly_breakout.json").read_text(encoding="utf-8"))
    raw = fixture["context"]
    htf, local, execution = raw["htfBias"], dict(raw["localRegime"]), raw["executionContext"]
    trade_flow, book_flow = {}, {}
    for suffix, horizon in raw["flowContext"]["horizons"].items():
        for key, source in (("imbalance", "tradeImbalance"), ("cvd", "cvdUsd"),
                            ("notional", "tradeNotionalUsd"), ("tradeCount", "tradeCount")):
            trade_flow[key+suffix] = horizon[source]
        for key, source in (("bestLevelOfiUsd", "ofiUsd"), ("normalizedOfi", "normalizedOfi"), ("eventCount", "ofiEventCount")):
            book_flow[key+suffix] = horizon[source]
    local["regime"] = LocalRegime(local["regime"])
    for key in ("direction", "parent_direction", "structure_1m", "structure_5m"):
        local[key] = Trend(local[key])
    ctx = replace(
        hourly_context(Action.SHORT), symbol=raw["symbol"], observed_at_ms=raw["observedAtMs"],
        last_price=raw["lastPrice"], legacy_trend=Trend(raw["legacyTrend"]),
        htf_bias=HTFBiasSnapshot(HTFBias(htf["bias"]), htf["strength"], Trend(htf["trend15m"]),
                               Trend(htf["trend1h"]), htf["alignment"], Trend(htf["legacyTrend"])),
        local_regime=LocalRegimeSnapshot(**local),
        flow=build_multi_horizon_flow_context(trade_flow, book_flow, observed_at_ms=raw["observedAtMs"]),
        execution=ExecutionContext(
            book_fresh=execution["bookFresh"], book_synced=execution["bookSynced"], book_age_seconds=execution["bookAgeSeconds"],
            candle_fresh=execution["candleFresh"], candle_age_seconds=execution["candleAgeSeconds"], spread_pct=execution["spreadPct"],
            best_bid=execution["bestBid"], best_ask=execution["bestAsk"], top5_bid_notional_usd=execution["top5BidNotionalUsd"],
            top5_ask_notional_usd=execution["top5AskNotionalUsd"], top5_depth_usd=execution["top5DepthUsd"],
            trade_buffer_seconds=execution["tradeBufferSeconds"],
        ),
    )
    assert fixture["recordedAssessment"]["blockers"] == ["breakout_htf_opposed"]
    assert fixture["breakHoldSeconds"] > LevelBreakoutStrategy.hold_without_retest_seconds
    assert ctx.flow.short_alignment.public() == raw["flowContext"]["shortAlignment"]
    assert assess(ctx, Action.SHORT).allowed
    assert not assess(ctx, Action.SHORT, confirmed=False).allowed
