import asyncio
from time import time

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candidate, Candle, OrderBook, Side, StrategyDecision, TradePlan
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
