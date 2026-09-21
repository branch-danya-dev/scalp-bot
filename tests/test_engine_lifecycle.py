import asyncio

import pytest
from time import time

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candidate, Candle, OrderBook, Side, StrategyDecision, TradePlan, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine


def candle() -> Candle:
    return Candle(0, 100, 101, 99, 100, 1, 100)


def book() -> OrderBook:
    return OrderBook(bids=[(99.99, 100)], asks=[(100.01, 100)])


def plan(symbol: str) -> TradePlan:
    return TradePlan(
        symbol=symbol,
        strategy="orderbook_density",
        side=Side.LONG,
        setup_entry=100,
        market_entry=100.01,
        stop=99.5,
        target=101,
        notional=500,
        leverage=0.5,
        max_loss_usd=2.5,
        expected_gross_profit=5,
        estimated_costs=0,
        expected_net_profit=5,
        expected_net_loss=2.5,
        net_reward_risk=2,
        entry_drift_pct=0,
        setup_id=f"density:{symbol}:100",
        strategy_details={},
    )


def make_engine(tmp_path, **overrides) -> TradingEngine:
    cfg = Settings(
        session_dir=str(tmp_path),
        min_net_profit_usd=0,
        min_net_reward_risk=0,
        taker_fee_rate=0,
        slippage_bps=0,
        max_entry_drift_bps=100,
        **overrides,
    )
    return TradingEngine(cfg)


def close_rest(engine: TradingEngine) -> None:
    asyncio.run(engine.rest.close())


def test_consumed_setup_is_blocked_until_wait_rearms_it(tmp_path) -> None:
    engine = make_engine(tmp_path, setup_rearm_seconds=0, setup_reset_wait_seconds=1)
    try:
        session = ActiveSymbolSession(symbol="AAAUSDT", candles=[candle()])
        session.consumed_setups["orderbook_density"] = "same-setup"
        session.cooldown_until["orderbook_density"] = 0

        assert engine._setup_blocked_reason(
            session, "orderbook_density", "same-setup", time()
        ) == "same setup already consumed"

        session.nontradeable_since["orderbook_density"] = time() - 2
        engine.sessions[session.symbol] = session
        engine._observe_wait_for_rearm(session, "orderbook_density", time())

        assert "orderbook_density" not in session.consumed_setups
        assert engine._setup_blocked_reason(
            session, "orderbook_density", "same-setup", time()
        ) is None
    finally:
        close_rest(engine)


def test_active_symbol_is_sticky_during_minimum_lifetime(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        active_symbol_min_seconds=600,
        active_symbol_idle_timeout_seconds=60,
    )
    try:
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            activated_at=now - 120,
            last_ranked_at=now - 120,
        )
        assert not engine._can_deactivate(session, now)

        session.activated_at = now - 1000
        session.last_ranked_at = now - 1000
        assert engine._can_deactivate(session, now)
    finally:
        close_rest(engine)


def test_central_arbiter_chooses_stronger_setup_instead_of_first_worker(tmp_path) -> None:
    engine = make_engine(tmp_path, max_leverage=1, risk_fraction=0.01)
    try:
        engine.running = True
        weak = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
        )
        strong = ActiveSymbolSession(
            symbol="BBBUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
        )
        weak.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.LONG,
            reasons=["weak"],
            confidence=0.55,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="weak-setup",
        )
        strong.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.LONG,
            reasons=["strong"],
            confidence=0.90,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="strong-setup",
        )
        engine.sessions = {"AAAUSDT": weak, "BBBUSDT": strong}
        engine.candidates = [
            Candidate("AAAUSDT", 200_000_000, 0, 100, activity_rank=2),
            Candidate("BBBUSDT", 200_000_000, 0, 100, activity_rank=1),
        ]

        engine._arbitrate_once()

        assert set(engine.broker.positions) == {"BBBUSDT"}
    finally:
        close_rest(engine)


def test_stop_button_finalizes_open_paper_position(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
        )
        engine.sessions["AAAUSDT"] = session
        engine.broker.open(plan("AAAUSDT"), book())
        engine.running = True

        engine.set_running(False)

        assert not engine.broker.positions
        assert engine.broker.closed_trades
        assert engine.broker.closed_trades[-1]["reason"] == "bot_stop"
    finally:
        close_rest(engine)


def test_central_arbiter_ignores_stale_market_snapshot(tmp_path) -> None:
    engine = make_engine(tmp_path, market_stale_seconds=1)
    try:
        engine.running = True
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time() - 10,
        )
        session.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.LONG,
            reasons=["stale"],
            confidence=0.99,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="stale-setup",
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate("AAAUSDT", 200_000_000, 0, 100, activity_rank=1),
        ]

        engine._arbitrate_once()

        assert not engine.broker.positions
    finally:
        close_rest(engine)



def test_replay_sampling_is_fast_only_for_engaged_market(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        replay_engaged_frame_seconds=1,
        replay_idle_frame_seconds=5,
    )
    try:
        session = ActiveSymbolSession(symbol="AAAUSDT", candles=[candle()])
        assert not engine._session_engaged(session)
        session.decisions["watch"] = StrategyDecision(
            strategy="watch",
            action=Action.WAIT,
            reasons=["watch"],
            confidence=0.6,
            watched_level=100,
        )
        assert engine._session_engaged(session)
    finally:
        close_rest(engine)


@pytest.mark.asyncio
async def test_duration_timer_auto_stops_and_finalizes_position(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        paper_run_duration_seconds=0.05,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
        )
        engine.sessions["AAAUSDT"] = session
        engine.broker.open(plan("AAAUSDT"), book())

        engine.set_running(True)
        await asyncio.sleep(0.12)

        assert not engine.running
        assert not engine.broker.positions
        assert engine.broker.closed_trades[-1]["reason"] == "duration_elapsed"
        assert engine._last_run_summary is not None
        assert engine._last_run_summary["reason"] == "duration_elapsed"
    finally:
        await engine.rest.close()



def test_new_rejection_and_density_states_keep_symbol_engaged(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        for state in ("persisting", "test", "defended", "reject", "reaction"):
            session = ActiveSymbolSession(symbol="AAAUSDT", candles=[candle()])
            session.decisions["stateful"] = StrategyDecision(
                strategy="stateful",
                action=Action.WAIT,
                reasons=["watch"],
                confidence=0.2,
                details={"state": state},
            )
            assert engine._session_engaged(session), state
    finally:
        close_rest(engine)


def test_countertrend_reaction_is_not_misclassified_as_lost_trend_context(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=99.9,
            trend=Trend.UP,
        )
        engine.sessions[session.symbol] = session
        p = plan("AAAUSDT")
        p.side = Side.SHORT
        p.stop = 100.5
        p.target = 99.0
        p.strategy = "weak_level_rejection"
        p.strategy_details = {
            "tradeMode": "countertrend_reaction",
            "allowRunner": False,
        }
        pos = engine.broker.open(p, book())
        pos.opened_at -= 10
        pos.unrealized_pnl = -0.1

        engine._maybe_strategy_invalidation(session)

        assert "AAAUSDT" in engine.broker.positions
    finally:
        close_rest(engine)
