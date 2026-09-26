from dataclasses import replace
from dataclasses import replace
from time import time

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candle, OrderBook, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy import PriceActionHypothesisStrategy
from scalp_bot.strategy.flow_context import build_multi_horizon_flow_context
from scalp_bot.strategy.market_context import ExecutionContext, MarketContext
from scalp_bot.strategy.regime import HTFBias, HTFBiasSnapshot, LocalRegime, LocalRegimeSnapshot


def scenario(direction=1):
    now = int(time() // 60) * 60_000 + 10_000
    rows = [Candle(now // 60_000 * 60_000 - (21-i)*60_000,
                   100, 100.05, 99.95, 100, 100, 10000) for i in range(21)]
    rows[-1] = Candle(rows[-1].start_ms, 100, 100.2, 99.9, 100.18, 160, 16000)
    if direction < 0:
        rows = [Candle(c.start_ms, 200-c.open, 200-c.low, 200-c.high,
                       200-c.close, c.volume, c.turnover) for c in rows]
    mid = 100.23 if direction > 0 else 99.77
    book = OrderBook(bids=[(mid-.005, 10000)], asks=[(mid+.005, 10000)])
    trend = Trend.UP if direction > 0 else Trend.DOWN
    flow_values = {"baselineReady":True}
    for seconds in (5, 15, 60):
        flow_values.update({f"imbalance{seconds}s":direction*.8, f"cvd{seconds}s":direction*8000,
                            f"notional{seconds}s":10000, f"tradeCount{seconds}s":100})
    flow = build_multi_horizon_flow_context(flow_values, {}, observed_at_ms=now)
    context = MarketContext("TESTUSDT", now, mid, trend,
        HTFBiasSnapshot(HTFBias.BULLISH if direction > 0 else HTFBias.BEARISH, 1, trend, trend, "aligned", trend),
        LocalRegimeSnapshot(LocalRegime.BULLISH_TREND if direction > 0 else LocalRegime.BEARISH_TREND,
                            trend, trend, .8, trend, trend, direction*.003, .003, .002, 1.5, .8, .0015, []),
        flow, None, None,
        ExecutionContext(True, True, .01, True, 10, book.spread_pct, book.best_bid, book.best_ask,
                         1e6, 1e6, 2e6, 90))
    return rows, book, context, flow_values


def evaluate(strategy, data, **overrides):
    rows, book, context, flow = data
    return strategy.evaluate(rows, book, Trend.FLAT, symbol="TESTUSDT",
                             market_context=context, observed_at_ms=context.observed_at_ms,
                             trade_flow=flow, **overrides)


@pytest.mark.parametrize("direction", [1, -1])
def test_beta_symmetric_confirmed_entry_geometry_and_no_reentry(direction):
    strategy = PriceActionHypothesisStrategy()
    data = scenario(direction)
    decision = evaluate(strategy, data)
    assert decision.action == (Action.LONG if direction > 0 else Action.SHORT)
    assert direction * (decision.entry - decision.stop) > 0
    assert direction * (decision.target - decision.entry) > 0
    assert decision.details["hypothesis"]["volumeRatio"] == 1.6
    assert decision.details["allowRunner"] is False
    repeat = evaluate(strategy, data)
    assert repeat.details["fireTrigger"] == decision.details["fireTrigger"]
    strategy.mark_opened("TESTUSDT", decision)
    assert not evaluate(strategy, data).tradeable


@pytest.mark.parametrize("case", ["unconfirmed", "future", "expired", "volume", "wick", "opposed_htf", "opposed_5m", "range", "stale", "unsynced", "flow", "warmup", "late"])
def test_beta_does_not_trade_unconfirmed_or_incompatible_evidence(case):
    rows, book, context, flow = scenario()
    if case == "unconfirmed": rows[-1].confirmed = False
    if case == "future": rows[-1].start_ms += 60_000
    if case == "expired": context = replace(context, observed_at_ms=context.observed_at_ms+60_000)
    if case == "volume": rows[-1].volume = 90
    if case == "wick": rows[-1].high = 101
    if case == "opposed_htf": context = replace(context, htf_bias=replace(context.htf_bias, trend_15m=Trend.DOWN))
    if case == "opposed_5m": context = replace(context, local_regime=replace(context.local_regime, structure_5m=Trend.DOWN))
    if case == "range": context = replace(context, local_regime=replace(context.local_regime, regime=LocalRegime.RANGE))
    if case == "stale": context = replace(context, execution=replace(context.execution, book_fresh=False))
    if case == "unsynced": context = replace(context, execution=replace(context.execution, book_synced=False))
    if case == "flow": context = replace(context, flow=scenario(-1)[2].flow)
    if case == "warmup": flow["baselineReady"] = False
    if case == "late": book = OrderBook(bids=[(100.5,1000)], asks=[(100.51,1000)])
    assert not evaluate(PriceActionHypothesisStrategy(), (rows,book,context,flow)).tradeable


def test_beta_waits_for_break_and_keeps_fixed_target():
    strategy = PriceActionHypothesisStrategy()
    rows, book, context, flow = scenario()
    waiting = evaluate(strategy, (rows,OrderBook(bids=[(100.18,1000)],asks=[(100.19,1000)]),context,flow))
    assert waiting.action == Action.WAIT
    assert waiting.details["state"] == "armed"
    entry = evaluate(strategy, (rows,book,context,flow))
    assert entry.tradeable
    assert entry.target == waiting.details["hypothesis"]["target"]
    assert entry.stop == waiting.details["hypothesis"]["invalidation"]


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("pattern", ["engulfing", "wick_rejection"])
def test_beta_recognizes_directional_patterns_symmetrically(direction, pattern):
    rows, book, context, flow = scenario(direction)
    previous = Candle(rows[-2].start_ms, 100.1, 100.15, 99.95, 100, 100, 10000)
    if pattern == "engulfing":
        bar = Candle(rows[-1].start_ms, 99.98, 100.2, 99.9, 100.18, 160, 16000)
    else:
        previous.close = 100.1
        bar = Candle(rows[-1].start_ms, 100.05, 100.2, 99.7, 100.18, 160, 16000)
    if direction < 0:
        previous, bar = [Candle(c.start_ms, 200-c.open, 200-c.low, 200-c.high,
                               200-c.close, c.volume, c.turnover) for c in (previous, bar)]
    rows[-2:] = [previous, bar]
    decision = evaluate(PriceActionHypothesisStrategy(), (rows, book, context, flow))
    assert decision.tradeable
    assert decision.details["hypothesis"]["pattern"] == pattern


@pytest.mark.parametrize("mid", [99.8, 100.5])
def test_beta_cancelled_or_missed_pattern_cannot_be_resurrected(mid):
    strategy = PriceActionHypothesisStrategy()
    rows, book, context, flow = scenario()
    invalid = OrderBook(bids=[(mid-.005, 10000)], asks=[(mid+.005, 10000)])
    assert not evaluate(strategy, (rows, invalid, context, flow)).tradeable
    assert not evaluate(strategy, (rows, book, context, flow)).tradeable
    strategy.reset("TESTUSDT")
    assert evaluate(strategy, (rows, book, context, flow)).tradeable


@pytest.mark.parametrize("direction", [1,-1])
@pytest.mark.parametrize("exit_reason", ["bot_stop", "stop", "target"])
@pytest.mark.asyncio
async def test_beta_ui_toggle_routes_real_signal_through_risk_and_paper(tmp_path, direction, exit_reason):
    cfg = Settings(session_dir=str(tmp_path), exchange_clock_enabled=False,
                   min_net_profit_usd=0, min_net_profit_equity_fraction=0,
                   enforce_min_net_profit_gate=False, enforce_net_reward_risk_gate=False,
                   enforce_winner_cost_share_gate=False)
    engine = TradingEngine(cfg)
    try:
        key = PriceActionHypothesisStrategy.key
        assert engine.strategy_enabled[key] is False
        engine.toggle_strategy(key, True)
        assert key in engine._build_trading_manifest()["strategies"]["tradeable"]
        rows, book, context, flow = scenario(direction)
        # Wide prior observed range supplies a real target independent of stop*R.
        rows = [replace(c, high=101, low=99) for c in rows[:-1]] + rows[-1:]
        session = ActiveSymbolSession("TESTUSDT", candles=rows, orderbook=book,
                                      last_price=book.mid, trend=context.legacy_trend,
                                      last_market_at=time(), last_book_at=time())
        session.market_context = context
        engine._route_scenario(session, rows)
        decision = evaluate(engine.strategies[key], (rows,book,session.market_context,flow))
        session.decisions[key] = engine._scenario_decision(session, decision)
        engine.sessions[session.symbol] = session
        engine.running = True
        engine._arbitrate_once()
        assert session.symbol in engine.broker.positions
        position = engine.broker.positions[session.symbol]
        assert position.strategy == key
        assert position.stop != position.target
        assert position.entry_fee_total_usd > 0
        assert position.strategy_details["allowRunner"] is False
        engine.toggle_strategy(key, False)
        assert engine.strategy_enabled[key] is False
        # Disabling new signals never abandons an existing position.
        assert session.symbol in engine.broker.positions
        if exit_reason == "bot_stop":
            engine._stop_trading("bot_stop")
        else:
            level = position.target if exit_reason == "target" else position.stop
            sign = direction if exit_reason == "target" else -direction
            mid = level + sign * .1
            exit_book = OrderBook(bids=[(mid-.005, 10000)], asks=[(mid+.005, 10000)])
            engine.broker.mark(session.symbol, mid, exit_book, trade_price=mid,
                               trade_notional_usd=100000, trade_side="Buy" if direction > 0 else "Sell")
        assert not engine.broker.positions
        assert engine.broker.closed_trades[-1]["strategy"] == key
        assert engine.broker.closed_trades[-1]["reason"] == exit_reason
        closed = engine.broker.closed_trades[-1]
        assert closed["fees"] > 0
        assert closed["netPnl"] == pytest.approx(closed["grossPnl"] - closed["fees"])
    finally:
        await engine.close()
