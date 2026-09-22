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
            last_book_at=time(),
        )
        strong = ActiveSymbolSession(
            symbol="BBBUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
            last_book_at=time(),
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
            last_book_at=time() - 10,
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



@pytest.mark.asyncio
async def test_start_survives_initial_scanner_failure(tmp_path) -> None:
    engine = make_engine(tmp_path)
    async def fail_scan() -> None:
        raise RuntimeError("temporary rate limit")
    engine._scan_once = fail_scan  # type: ignore[method-assign]

    try:
        await engine.start()

        assert engine._tasks
        assert any(event["event"] == "startup_scan_error" for event in engine.events)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_promote_symbol_survives_bootstrap_failure(tmp_path) -> None:
    engine = make_engine(tmp_path)
    async def fail_bootstrap(symbol: str) -> None:
        raise RuntimeError("temporary bootstrap rate limit")
    engine._bootstrap_symbol = fail_bootstrap  # type: ignore[method-assign]

    try:
        await engine._promote_symbol("AAAUSDT", time())

        assert "AAAUSDT" not in engine.sessions
        assert any(
            event["event"] == "symbol_bootstrap_error"
            and event["symbol"] == "AAAUSDT"
            for event in engine.events
        )
    finally:
        await engine.rest.close()



def test_opportunity_score_prefers_setup_quality_not_geometry_rr(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        high_quality = StrategyDecision(
            strategy="test",
            action=Action.LONG,
            reasons=["quality"],
            confidence=0.70,
            details={"setupQuality": 0.90},
        )
        low_quality = StrategyDecision(
            strategy="test",
            action=Action.LONG,
            reasons=["geometry"],
            confidence=0.85,
            details={"setupQuality": 0.40},
        )
        assert engine._opportunity_score(high_quality, 5, 50) > engine._opportunity_score(low_quality, 1, 50)
    finally:
        close_rest(engine)


def test_density_only_invalidates_on_explicit_price_flow_failure(tmp_path) -> None:
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
        p.strategy = "orderbook_density"
        p.strategy_details = {"tradeMode": "trend_following", "allowRunner": True}
        pos = engine.broker.open(p, book())
        pos.opened_at -= 10
        pos.unrealized_pnl = -0.1

        session.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.WAIT,
            reasons=["wall removed after defense"],
            details={"state": "reaction", "positionInvalidated": False},
        )
        engine._maybe_strategy_invalidation(session)
        assert "AAAUSDT" in engine.broker.positions

        session.decisions["orderbook_density"].details["positionInvalidated"] = True
        engine._maybe_strategy_invalidation(session)
        assert "AAAUSDT" not in engine.broker.positions
        assert engine.broker.closed_trades[-1]["reason"] == "density_price_flow_invalidated"
    finally:
        close_rest(engine)



def test_activity_score_can_break_close_setup_quality_tie(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        decision = StrategyDecision(
            strategy="test",
            action=Action.LONG,
            reasons=["same quality"],
            confidence=0.70,
            details={"setupQuality": 0.70},
        )
        hot = engine._opportunity_score(decision, 5, 90)
        quiet = engine._opportunity_score(decision, 5, 10)
        assert hot > quiet
    finally:
        close_rest(engine)



@pytest.mark.asyncio
async def test_bootstrap_loads_direct_multi_timeframe_context(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        bootstrap_1m_candles=720,
        bootstrap_5m_candles=576,
        bootstrap_15m_candles=480,
        bootstrap_1h_candles=336,
    )
    calls: list[tuple[str, int]] = []

    async def fake_klines(
        symbol: str,
        interval: str,
        limit: int = 240,
    ) -> list[Candle]:
        calls.append((interval, limit))
        count = {"1": 60, "5": 60, "15": 60, "60": 60}[interval]
        return [
            Candle(
                i * 60_000,
                100 + i * 0.01,
                100.2 + i * 0.01,
                99.8 + i * 0.01,
                100.1 + i * 0.01,
                10,
                1000,
            )
            for i in range(count)
        ]

    engine.rest.klines = fake_klines  # type: ignore[method-assign]
    try:
        await engine._bootstrap_symbol("TESTUSDT")
        session = engine.sessions["TESTUSDT"]

        assert ("1", 720) in calls
        assert ("5", 576) in calls
        assert ("15", 480) in calls
        assert ("60", 336) in calls
        assert session.context_5m
        assert session.context_15m
        assert session.context_1h
    finally:
        await engine.rest.close()
