import asyncio

import pytest
from time import time

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candidate, Candle, OrderBook, Side, StrategyDecision, TradePlan, TradeTick, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine


def candle() -> Candle:
    return Candle(0, 100, 101, 99, 100, 1, 100)


def book(
    bid: float = 99.99,
    ask: float = 100.01,
) -> OrderBook:
    return OrderBook(bids=[(bid, 100)], asks=[(ask, 100)])


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



def test_density_engagement_records_full_research_book_depth(tmp_path) -> None:
    engine = make_engine(tmp_path, orderbook_depth=1000)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
        )
        assert engine._research_book_depth(session, None) == 50

        session.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.WAIT,
            reasons=["wall found"],
            details={"state": "found"},
        )
        assert engine._research_book_depth(session, None) == 1000

        p = plan("AAAUSDT")
        p.strategy = "orderbook_density"
        position = engine.broker.open(p, book())
        session.decisions.clear()
        assert engine._research_book_depth(session, position) == 1000
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
        session.last_market_at = time()
        session.last_book_at = time()
        session.book_synced = True
        engine.sessions["AAAUSDT"] = session
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]
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

        pos.partial_taken = True
        pos.unrealized_pnl = 0.1
        session.decisions["orderbook_density"].details["positionInvalidated"] = True
        engine._maybe_strategy_invalidation(session)
        assert "AAAUSDT" not in engine.broker.positions
        assert engine.broker.closed_trades[-1]["reason"] == "density_price_flow_invalidated"
    finally:
        close_rest(engine)



def test_breakout_premise_invalidates_even_while_trade_is_positive(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=99.95,
            trend=Trend.UP,
        )
        engine.sessions[session.symbol] = session
        p = plan("AAAUSDT")
        p.strategy = "level_breakout"
        p.strategy_details = {
            "zone": {"low": 99.80, "high": 100.00},
            "tradeMode": "trend_following",
            "allowRunner": True,
        }
        pos = engine.broker.open(p, book())
        pos.opened_at -= 10
        pos.unrealized_pnl = 0.1

        session.decisions["level_breakout"] = StrategyDecision(
            strategy="level_breakout",
            action=Action.WAIT,
            reasons=["back inside"],
            details={"state": "break"},
        )

        engine._maybe_strategy_invalidation(session)
        assert "AAAUSDT" in engine.broker.positions

        pos.strategy_details["_breakoutBackInsideSinceMs"] -= 4_000
        engine._maybe_strategy_invalidation(session)

        assert "AAAUSDT" not in engine.broker.positions
        assert (
            engine.broker.closed_trades[-1]["reason"]
            == "breakout_failed_back_inside"
        )
    finally:
        close_rest(engine)


def test_breakout_retest_does_not_immediately_invalidate(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(99.95, 99.96),
            last_price=99.95,
            trend=Trend.UP,
        )
        engine.sessions[session.symbol] = session
        p = plan("AAAUSDT")
        p.strategy = "level_breakout"
        p.strategy_details = {
            "zone": {"low": 99.80, "high": 100.00},
            "tradeMode": "trend_following",
            "allowRunner": True,
        }
        pos = engine.broker.open(p, book())
        pos.opened_at -= 10
        session.decisions["level_breakout"] = StrategyDecision(
            strategy="level_breakout",
            action=Action.WAIT,
            reasons=["retest"],
            details={"state": "break"},
        )

        engine._maybe_strategy_invalidation(session)

        assert "AAAUSDT" in engine.broker.positions
        assert "_breakoutBackInsideSinceMs" in pos.strategy_details
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
                confirmed=(i != count - 1),
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
        assert all(x.confirmed for x in session.context_5m)
        assert all(x.confirmed for x in session.context_15m)
        assert all(x.confirmed for x in session.context_1h)
        assert len(session.context_15m) == 59
    finally:
        await engine.rest.close()



def test_active_session_trade_buffer_has_no_count_limit() -> None:
    session = ActiveSymbolSession(symbol="AAAUSDT")
    assert session.trades.maxlen is None
    for i in range(3_000):
        session.trades.append(
            TradeTick(
                ts_ms=100_000 + i,
                price=100,
                size=0.01,
                side="Buy",
            )
        )
    assert len(session.trades) == 3_000



def test_central_arbiter_ignores_stale_book_even_when_market_is_fresh(
    tmp_path,
) -> None:
    engine = make_engine(tmp_path, book_stale_seconds=1)
    try:
        engine.running = True
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
            last_book_at=time() - 10,
            book_stale_after_seconds=1,
            book_synced=True,
        )
        session.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.LONG,
            reasons=["stale book"],
            confidence=0.99,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="stale-book-setup",
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            ),
        ]

        engine._arbitrate_once()

        assert not engine.broker.positions
        assert session.book_is_fresh(time()) is False
    finally:
        close_rest(engine)


@pytest.mark.asyncio
async def test_density_is_not_evaluated_when_book_is_stale(tmp_path) -> None:
    engine = make_engine(tmp_path, book_stale_seconds=1)
    try:
        rows = [
            Candle(
                i * 60_000,
                100,
                100.2,
                99.8,
                100,
                10,
                1000,
            )
            for i in range(80)
        ]
        context = [
            Candle(
                i * 900_000,
                100 + i * 0.01,
                100.3 + i * 0.01,
                99.7 + i * 0.01,
                100.1 + i * 0.01,
                10,
                1000,
            )
            for i in range(80)
        ]
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=rows,
            context_15m=context,
            orderbook=book(),
            last_price=100,
            last_book_at=time() - 5,
            book_stale_after_seconds=1,
            book_synced=True,
        )

        await engine._evaluate(session)

        decision = session.decisions["orderbook_density"]
        assert decision.action == Action.WAIT
        assert decision.details["state"] == "stale_book"
        assert decision.details["positionInvalidated"] is False
        assert decision.details["bookHealth"]["fresh"] is False
    finally:
        await engine.rest.close()


def test_book_health_is_exposed_in_market_snapshot(tmp_path) -> None:
    engine = make_engine(tmp_path, book_stale_seconds=1)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_book_at=time(),
            book_stale_after_seconds=1,
            book_synced=True,
        )

        snapshot = session.market_snapshot()

        assert snapshot["bookHealth"]["fresh"] is True
        assert snapshot["bookHealth"]["synced"] is True
        assert snapshot["bookHealth"]["bidLevels"] > 0
        assert snapshot["bookHealth"]["askLevels"] > 0

        session.book_synced = False
        assert session.book_health()["fresh"] is False
    finally:
        close_rest(engine)


def test_book_flow_snapshot_expires_against_observation_clock() -> None:
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        orderbook=book(),
    )
    session.record_book_flow(100_000, 500.0)

    fresh = session.book_flow_snapshot(103_000)
    stale = session.book_flow_snapshot(106_000)

    assert fresh["bestLevelOfiUsd5s"] == 500.0
    assert stale["bestLevelOfiUsd5s"] == 0.0
    assert stale["bestLevelOfiUsd15s"] == 500.0


def test_executable_book_move_can_trigger_target_without_new_trade_tick(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100.0,
        )
        engine.sessions[session.symbol] = session
        p = plan("AAAUSDT")
        p.target = 101.0
        engine.broker.open(p, session.orderbook)

        session.orderbook = OrderBook(
            bids=[(101.0, 100)],
            asks=[(101.01, 100)],
        )
        engine._mark_position_from_book(session)

        assert "AAAUSDT" not in engine.broker.positions
        assert engine.broker.closed_trades[-1]["reason"] == "target"
    finally:
        close_rest(engine)


@pytest.mark.asyncio
async def test_strategies_receive_only_confirmed_1m_candles(tmp_path) -> None:
    engine = make_engine(tmp_path)
    seen: list[Candle] = []

    class CaptureStrategy:
        key = "capture"
        label = "capture"

        def evaluate(
            self,
            candles,
            book,
            trend,
            **kwargs,
        ):
            seen.extend(candles)
            return StrategyDecision(
                strategy=self.key,
                action=Action.WAIT,
                reasons=["capture"],
            )

        def reset(self, symbol: str) -> None:
            return None

        def manage_position(self, **kwargs):
            return None

    engine.strategies = {"capture": CaptureStrategy()}  # type: ignore[assignment]
    engine.strategy_enabled = {"capture": True}
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=[
            Candle(0, 100, 101, 99, 100, 1, 100, confirmed=True),
            Candle(
                60_000,
                100,
                150,
                50,
                140,
                1000,
                140_000,
                confirmed=False,
            ),
        ],
        orderbook=book(),
        last_price=100,
    )
    engine.sessions[session.symbol] = session

    try:
        await engine._evaluate(session)
        assert len(seen) == 1
        assert seen[0].confirmed is True
        assert seen[0].high == 101
    finally:
        await engine.rest.close()


def test_paper_run_cannot_start_without_live_market_data(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        with pytest.raises(
            RuntimeError,
            match="cannot start paper run",
        ):
            engine.set_running(True)
        assert engine.running is False
    finally:
        close_rest(engine)


def test_market_health_requires_fresh_book_and_live_session(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
            last_book_at=time(),
            book_synced=True,
            book_stale_after_seconds=2,
        )
        engine.sessions[session.symbol] = session

        health = engine.market_health()

        assert health["ready"] is True
        assert health["liveSymbolCount"] == 1
        assert engine.start_block_reason() is None
    finally:
        close_rest(engine)



def test_decision_trace_identifies_market_object_and_wait_condition(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            trend=Trend.UP,
        )
        decision = StrategyDecision(
            strategy="weak_level_rejection",
            action=Action.WAIT,
            reasons=["waiting for local tape reversal"],
            confidence=0.58,
            watched_level=100.0,
            details={
                "state": "reject",
                "zone": {
                    "kind": "support",
                    "low": 99.95,
                    "high": 100.05,
                    "touches": 2,
                },
                "trendAligned": True,
                "recentLevelFlow": {
                    "imbalance": -0.02,
                    "tradeCount": 4,
                },
            },
        )

        engine._record_decision_if_changed(session, decision)

        event = engine.events[0]
        trace = event["payload"]["trace"]
        assert event["event"] == "decision"
        assert trace["strategy"] == "weak_level_rejection"
        assert trace["state"] == "reject"
        assert trace["trend"] == "up"
        assert trace["object"]["type"] == "horizontal_zone"
        assert trace["object"]["low"] == pytest.approx(99.95)
        assert trace["object"]["high"] == pytest.approx(100.05)
        assert "fresh local tape reversal at the level" in trace["waitingFor"]
        assert "trend direction aligned" in trace["confirmed"]
        assert trace["evidence"]["recentLevelFlow"]["tradeCount"] == 4
    finally:
        close_rest(engine)


def test_market_snapshot_exposes_trace_for_current_decisions(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            trend=Trend.DOWN,
        )
        session.decisions["orderbook_density"] = StrategyDecision(
            strategy="orderbook_density",
            action=Action.WAIT,
            reasons=["waiting for actual touch"],
            details={
                "state": "approach",
                "wallSide": "ask",
                "wallPrice": 101.0,
                "notionalUsd": 75_000,
                "strengthMultiple": 6.2,
            },
        )

        snapshot = session.market_snapshot()
        trace = snapshot["decisions"]["orderbook_density"]["trace"]

        assert trace["object"]["type"] == "orderbook_wall"
        assert trace["object"]["price"] == pytest.approx(101.0)
        assert trace["state"] == "approach"
        assert "actual trade touch of the wall" in trace["waitingFor"]
    finally:
        close_rest(engine)



def test_market_snapshot_exposes_multi_timeframe_chart_series() -> None:
    now_ms = 120_000
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=[
            Candle(0, 100, 101, 99, 100.5, 10, 1000),
            Candle(60_000, 100.5, 102, 100, 101, 12, 1200),
        ],
        context_5m=[
            Candle(0, 100, 102, 99, 101, 20, 2000),
        ],
        context_15m=[
            Candle(0, 100, 103, 98, 102, 30, 3000),
        ],
        context_1h=[
            Candle(0, 100, 104, 97, 103, 40, 4000),
        ],
    )
    session.trades.extend([
        TradeTick(100_000, 100.0, 1, "Buy"),
        TradeTick(103_000, 101.0, 1, "Buy"),
        TradeTick(108_000, 100.5, 1, "Sell"),
    ])

    series = session.chart_series(now_ms)

    assert set(series) == {"5s", "15s", "1m", "5m", "10m", "15m", "1h"}
    assert len(series["1m"]) == 2
    assert len(series["5m"]) == 1
    assert len(series["10m"]) == 1
    assert len(series["15m"]) == 1
    assert len(series["1h"]) == 1
    assert len(series["5s"]) >= 2
    assert series["5s"][0]["open"] == pytest.approx(100.0)



def test_density_context_exposes_wall_flow_and_book_focus() -> None:
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        orderbook=OrderBook(
            bids=[(99.99 - i * 0.01, 10) for i in range(20)],
            asks=[(100.01 + i * 0.01, 10) for i in range(20)],
        ),
    )
    session.decisions["orderbook_density"] = StrategyDecision(
        strategy="orderbook_density",
        action=Action.WAIT,
        reasons=["wall test"],
        details={
            "state": "test",
            "wallSide": "ask",
            "wallPrice": 100.10,
            "notionalUsd": 75_000,
            "strengthMultiple": 6.5,
            "remainingRatio": 0.88,
            "attackNotional5s": 12_000,
            "attackRatio": 0.16,
            "depletionPerSecond": 0.03,
            "replenishmentRatio": 0.12,
            "absorptionObserved": True,
            "wallPresent": True,
            "recentLevelFlow": {
                "imbalance": -0.11,
                "tradeCount": 8,
            },
        },
    )
    session.record_book_flow(100_000, -5_000)

    context = session.density_context(101_000)

    assert context is not None
    assert context["wallPrice"] == pytest.approx(100.10)
    assert context["state"] == "test"
    assert context["absorptionObserved"] is True
    assert context["recentLevelFlow"]["tradeCount"] == 8
    assert context["bookFlow"]["bestLevelOfiUsd5s"] == pytest.approx(-5_000)
    assert context["focusLevels"]



def test_strategy_runtime_stats_track_decisions_rejects_and_closed_trade(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            trend=Trend.UP,
        )
        decision = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["confirmed"],
            entry=100,
            stop=99,
            target=102,
        )
        engine._record_decision_if_changed(session, decision)
        engine._risk_reject_if_changed(
            session,
            decision,
            "test rejection",
        )

        stats = engine.strategy_stats["trend_structure"]
        assert stats["decisions"] == 1
        assert stats["tradeableSignals"] == 1
        assert stats["riskRejects"] == 1

        engine._handle_broker_events(
            session,
            [{
                "event": "trade_closed",
                "strategy": "trend_structure",
                "setupId": "x",
                "netPnl": 3.5,
            }],
        )
        assert stats["tradesClosed"] == 1
        assert stats["wins"] == 1
        assert stats["losses"] == 0
        assert stats["netPnl"] == pytest.approx(3.5)
    finally:
        close_rest(engine)



def test_arbiter_records_shadow_economics_without_blocking_trade(tmp_path) -> None:
    engine = TradingEngine(Settings(
        session_dir=str(tmp_path),
        min_net_profit_usd=1.0,
        min_net_profit_equity_fraction=0.001,
        enforce_min_net_profit_gate=False,
        min_net_reward_risk=1.15,
        enforce_net_reward_risk_gate=False,
        taker_fee_rate=0.00055,
        slippage_bps=1.0,
        max_entry_drift_bps=100,
        max_position_leverage=5.0,
    ))
    try:
        engine.running = True
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=now,
            last_book_at=now,
            book_synced=True,
        )
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["research shadow case"],
            confidence=0.8,
            entry=100.0,
            stop=99.45,
            target=100.24,
            setup_id="shadow-case",
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate("AAAUSDT", 200_000_000, 0, 100, activity_rank=1)
        ]
        engine._arbitrate_once()
        assert "AAAUSDT" in engine.broker.positions
        assert any(event["event"] == "economic_shadow" for event in engine.events)
    finally:
        close_rest(engine)



def test_risk_reject_event_includes_diagnostics_snapshot(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
        )
        decision = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["test"],
            entry=100,
            stop=99.5,
            target=100.1,
        )
        engine._risk_reject_if_changed(
            session,
            decision,
            "test reject",
            diagnostics={"netAtTargetUsd": 0.5},
        )
        payload = engine.events[0]["payload"]
        assert payload["diagnostics"]["netAtTargetUsd"] == 0.5
        assert payload["diagnostics"]["bestBid"] == pytest.approx(99.99)
        assert payload["diagnostics"]["target"] == pytest.approx(100.1)
    finally:
        close_rest(engine)
