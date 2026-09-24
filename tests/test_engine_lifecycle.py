import asyncio

import pytest
from time import time

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candidate, Candle, OrderBook, Side, StrategyDecision, TradePlan, TradeTick, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.research_policy import (
    create_policy_manifest,
    extract_policy_candidates,
    write_policy_manifest,
)
from scalp_bot.strategy.market_context import (
    ExecutionContext,
    MarketContext,
    StructureContext,
)
from scalp_bot.strategy.structure import StructuralLevel


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
    overrides.setdefault("absolute_min_net_reward_risk", 0.0)
    overrides.setdefault("trend_structure_enabled", True)
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


def policy_file(
    tmp_path,
    *,
    allow_enforce: bool,
) -> str:
    stability = {
        "featureEffects": [
            {
                "strategy": "trend_structure",
                "side": "long",
                "regime": "bullish_trend",
                "dimension": "flowAlignment",
                "value": "short_term_reversal",
                "status": "stable_negative",
                "validationCandidate": True,
                "selectedSamples": 20,
                "comparatorSamples": 20,
                "sessionsWithSelectedValue": 5,
                "pooledDeltaAllInR": -0.4,
                "maxSessionSampleShare": 0.25,
                "comparisonSessions": 5,
                "medianSessionDeltaAllInR": -0.3,
                "leaveOneSessionOutFolds": 5,
                "leaveOneSessionOutSignAgreementRate": 1.0,
            }
        ],
        "fixedNetRewardRiskThresholds": [],
        "thresholdSelectionHoldout": [],
    }
    candidates = extract_policy_candidates(
        stability
    )
    catalog = {
        "source": {
            "stabilitySha256": "test",
        },
        "candidates": candidates,
    }
    manifest = create_policy_manifest(
        catalog,
        [candidates[0]["candidateId"]],
        version=1,
        reason="engine integration test",
        allow_enforce=allow_enforce,
    )
    path = tmp_path / (
        "policy-enforce.json"
        if allow_enforce
        else "policy-shadow.json"
    )
    write_policy_manifest(
        path,
        manifest,
    )
    return str(path)


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


def test_central_arbiter_uses_shared_priority_not_playbook_confidence(tmp_path) -> None:
    engine = make_engine(tmp_path, max_leverage=1, risk_fraction=0.01)
    try:
        engine.running = True
        first = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
            last_book_at=time(),
        )
        second = ActiveSymbolSession(
            symbol="BBBUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=time(),
            last_book_at=time(),
        )
        first.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["high playbook confidence"],
            confidence=0.99,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="first-setup",
            details={"setupQuality": 0.99},
        )
        second.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["low playbook confidence"],
            confidence=0.10,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="second-setup",
            details={"setupQuality": 0.10},
        )
        engine.sessions = {"AAAUSDT": first, "BBBUSDT": second}
        engine.candidates = [
            Candidate("AAAUSDT", 200_000_000, 0, 100, activity_rank=2),
            Candidate("BBBUSDT", 200_000_000, 0, 100, activity_rank=1),
        ]

        engine._arbitrate_once()

        # Semantic/economic dimensions are equal, so the later shared
        # activity-rank tie-break chooses BBB despite its lower setupQuality.
        assert set(engine.broker.positions) == {"BBBUSDT"}
    finally:
        close_rest(engine)


def test_research_policy_shadow_audits_without_blocking_trade(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        max_leverage=1,
        risk_fraction=0.01,
        research_policy_mode="shadow",
        research_policy_file=policy_file(
            tmp_path,
            allow_enforce=False,
        ),
    )
    try:
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=now,
            last_book_at=now,
        )
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["ready"],
            confidence=0.8,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="policy-shadow",
            details={
                "decisionContext": {
                    "localRegime": "bullish_trend",
                },
                "flowAlignment": {
                    "classification": "short_term_reversal",
                },
            },
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]

        engine._arbitrate_once()

        assert set(engine.broker.positions) == {"AAAUSDT"}
        events = [
            event
            for event in engine.events
            if event["event"] == "research_policy_shadow"
        ]
        assert events
        assessment = events[-1]["payload"]["assessment"]
        assert assessment["wouldBlock"] is True
        assert assessment["blocked"] is False
        assert assessment["mode"] == "shadow"
    finally:
        close_rest(engine)


def test_research_policy_enforce_blocks_exact_validated_context(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        max_leverage=1,
        risk_fraction=0.01,
        research_policy_mode="enforce",
        research_policy_file=policy_file(
            tmp_path,
            allow_enforce=True,
        ),
    )
    try:
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=now,
            last_book_at=now,
        )
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["ready"],
            confidence=0.8,
            entry=100,
            stop=99.5,
            target=101,
            watched_level=99.8,
            setup_id="policy-enforce",
            details={
                "decisionContext": {
                    "localRegime": "bullish_trend",
                },
                "flowAlignment": {
                    "classification": "short_term_reversal",
                },
            },
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]

        engine._arbitrate_once()

        assert not engine.broker.positions
        events = [
            event
            for event in engine.events
            if event["event"] == "research_policy_blocked"
        ]
        assert events
        assessment = events[-1]["payload"]["assessment"]
        assert assessment["blocked"] is True
        assert assessment["mode"] == "enforce"
    finally:
        close_rest(engine)


def test_arbiter_blocks_trend_long_into_mature_resistance(tmp_path) -> None:
    engine = make_engine(tmp_path, max_leverage=1, risk_fraction=0.01)
    try:
        now = time()
        resistance = StructuralLevel(
            kind="resistance",
            low=100.20,
            high=100.30,
            touches=4,
            timeframe="5m",
            score=0.9,
            distinct_approaches=4,
            lifecycle="worked",
            generation_id="R:test:g1",
        )
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=now,
            last_book_at=now,
        )
        session.market_context = MarketContext(
            symbol="AAAUSDT",
            observed_at_ms=int(now * 1000),
            last_price=100.0,
            legacy_trend=Trend.UP,
            htf_bias=None,
            local_regime=None,
            flow=None,
            liquidity=None,
            structure=StructureContext(
                reference_price=100.0,
                level_count=1,
                trendline_count=0,
                nearest_support=None,
                nearest_resistance=resistance,
                support_distance_pct=None,
                resistance_distance_pct=0.0025,
                support_trendline=None,
                resistance_trendline=None,
            ),
            execution=ExecutionContext(
                book_fresh=True,
                book_synced=True,
                book_age_seconds=0.1,
                candle_fresh=True,
                candle_age_seconds=1.0,
                spread_pct=0.0002,
                best_bid=99.99,
                best_ask=100.01,
                top5_bid_notional_usd=9999.0,
                top5_ask_notional_usd=10001.0,
                top5_depth_usd=20000.0,
                trade_buffer_seconds=60.0,
            ),
        )
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["continuation"],
            confidence=0.99,
            entry=100.0,
            stop=99.5,
            target=101.0,
            watched_level=99.8,
            setup_id="blocked-by-resistance",
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]

        engine._arbitrate_once()

        assert not engine.broker.positions
        blocked = [
            event
            for event in engine.events
            if event["event"] == "arbiter_blocked"
        ]
        assert blocked
        assert (
            "mature_structural_obstacle_before_first_take"
            in blocked[0]["payload"]["blockers"]
        )
    finally:
        close_rest(engine)


def test_arbiter_waits_when_viable_playbooks_conflict_on_direction(tmp_path) -> None:
    engine = make_engine(tmp_path, max_leverage=1, risk_fraction=0.01)
    try:
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            last_market_at=now,
            last_book_at=now,
        )
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["continuation"],
            confidence=0.9,
            entry=100.0,
            stop=99.5,
            target=101.0,
            watched_level=100.0,
            setup_id="trend-long",
        )
        session.decisions["weak_level_rejection"] = StrategyDecision(
            strategy="weak_level_rejection",
            action=Action.SHORT,
            reasons=["rejection"],
            confidence=0.9,
            entry=100.0,
            stop=100.5,
            target=99.0,
            watched_level=100.1,
            setup_id="rejection-short",
        )
        engine.sessions = {"AAAUSDT": session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]

        engine._arbitrate_once()

        assert not engine.broker.positions
        blocked = [
            event
            for event in engine.events
            if event["event"] == "arbiter_blocked"
        ]
        assert len(blocked) >= 2
        assert all(
            "opposing_playbook_conflict"
            in event["payload"]["blockers"]
            for event in blocked[-2:]
        )
    finally:
        close_rest(engine)


def test_live_strategy_stats_split_side_and_local_regime() -> None:
    stats = {
        "sideRegime": {
            "long": {},
            "short": {},
        }
    }
    event = {
        "side": "long",
        "grossPnl": -4.0,
        "fees": 1.0,
        "netPnl": -5.0,
        "mfeR": 0.2,
        "maeR": 0.8,
        "strategyDetails": {
            "decisionContext": {
                "localRegime": "bullish_trend",
            }
        },
    }

    TradingEngine._update_side_regime_stats(
        stats,
        event,
    )

    overall = stats["sideRegime"]["long"]["all"]
    regime = stats["sideRegime"]["long"]["bullish_trend"]
    assert overall["trades"] == 1
    assert overall["losses"] == 1
    assert overall["netPnl"] == pytest.approx(-5.0)
    assert overall["averageMfeR"] == pytest.approx(0.2)
    assert overall["averageMaeR"] == pytest.approx(0.8)
    assert regime["trades"] == 1
    assert regime["winRate"] == 0


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
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
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
        for state in (
            "persisting",
            "pullback",
            "armed",
            "test",
            "reclaim",
            "defended",
            "reject",
            "reaction",
        ):
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


def test_book_move_alone_does_not_claim_maker_target_fill(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        maker_fill_confirmation_bps=0.5,
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

        assert "AAAUSDT" in engine.broker.positions

        session.last_price = 101.01
        engine._mark_position_from_book(
            session,
            trade_price=101.01,
        )
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



def test_chart_series_adds_live_bucket_to_higher_timeframes() -> None:
    now_ms = 3_690_000
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=[
            Candle(3_600_000, 100, 101, 99.5, 100.5, 10, 1000, True),
            Candle(3_660_000, 100.5, 101.5, 100.2, 101.0, 12, 1200, False),
        ],
        context_5m=[
            Candle(3_300_000, 99, 100.5, 98.8, 100, 20, 2000, True),
        ],
        context_15m=[
            Candle(2_700_000, 98, 101, 97.5, 100, 30, 3000, True),
        ],
        context_1h=[
            Candle(0, 95, 102, 94, 100, 40, 4000, True),
        ],
    )

    series = session.chart_series(now_ms)

    for timeframe in ("5m", "15m", "1h"):
        live = series[timeframe][-1]
        assert live["time"] == 3_600
        assert live["open"] == pytest.approx(100.0)
        assert live["close"] == pytest.approx(101.0)
        assert live["confirmed"] is False


def test_chart_series_marks_10m_bucket_from_clock_not_1m_flags() -> None:
    now_ms = 5 * 60_000
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=[
            Candle(0, 100, 101, 99, 100.5, 10, 1000, True),
            Candle(60_000, 100.5, 102, 100, 101, 12, 1200, True),
        ],
    )

    live = session.chart_series(now_ms)["10m"][-1]

    assert live["time"] == 0
    assert live["confirmed"] is False


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
        absolute_min_net_reward_risk=0.0,
        trend_structure_enabled=True,
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



def test_strategy_expectancy_remains_observational_until_sample_ready(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        strategy_expectancy_min_samples=3,
        enforce_strategy_expectancy_gate=True,
        trend_structure_min_expectancy_r=0.0,
    )
    try:
        engine.expectancy.record(
            "trend_structure",
            net_pnl_usd=-2.0,
            initial_risk_usd=1.0,
        )
        snapshot = engine.expectancy.snapshot(
            "trend_structure",
            min_samples=3,
            minimum_expectancy_r=0.0,
        )
        assert snapshot["sampleReady"] is False
        assert snapshot["status"] == "insufficient_samples"

        engine.expectancy.record(
            "trend_structure",
            net_pnl_usd=-1.0,
            initial_risk_usd=1.0,
        )
        engine.expectancy.record(
            "trend_structure",
            net_pnl_usd=0.5,
            initial_risk_usd=1.0,
        )
        snapshot = engine.expectancy.snapshot(
            "trend_structure",
            min_samples=3,
            minimum_expectancy_r=0.0,
        )
        assert snapshot["sampleReady"] is True
        assert snapshot["status"] == "negative"
        assert snapshot["expectancyR"] < 0
    finally:
        close_rest(engine)



def test_arbiter_uses_pending_maker_entry_for_tradeable_playbook_and_fills_after_trade_through(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        passive_entry_enabled=True,
        maker_fill_confirmation_bps=0.5,
        enforce_winner_cost_share_gate=False,
        enforce_stop_cost_share_gate=False,
    )
    try:
        engine.running = True
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100,
            trend=Trend.UP,
            last_market_at=now,
            last_book_at=now,
            book_synced=True,
        )
        session.decisions["weak_level_rejection"] = StrategyDecision(
            strategy="weak_level_rejection",
            action=Action.LONG,
            reasons=["confirmed rejection"],
            confidence=0.9,
            entry=100.0,
            stop=99.5,
            target=101.0,
            setup_id="rejection-passive-1",
            details={"allowRunner": True, "state": "reaction"},
        )
        engine.sessions = {session.symbol: session}
        engine.candidates = [
            Candidate("AAAUSDT", 200_000_000, 0, 100, activity_rank=1)
        ]
        engine._arbitrate_once()
        assert "AAAUSDT" in engine.broker.pending_entries
        assert "AAAUSDT" not in engine.broker.positions
        assert any(event["event"] == "entry_pending" for event in engine.events)
        pending = engine.broker.pending_entries["AAAUSDT"]
        session.last_price = pending.limit_price * (
            1 - engine.config.maker_fill_confirmation_bps / 10_000
        )
        engine._mark_execution_from_market(
            session,
            trade_ts_ms=int(time() * 1000),
        )
        assert "AAAUSDT" not in engine.broker.pending_entries
        assert "AAAUSDT" in engine.broker.positions
        assert any(event["event"] == "trade_opened" for event in engine.events)
        assert engine.strategy_stats["weak_level_rejection"]["tradesOpened"] == 1
    finally:
        close_rest(engine)



def _kline_message_row(
    start_ms: int,
    close: float,
    *,
    confirm: bool,
) -> dict:
    return {
        "start": start_ms,
        "open": str(close - 0.1),
        "high": str(close + 0.2),
        "low": str(close - 0.2),
        "close": str(close),
        "volume": "10",
        "turnover": str(close * 10),
        "confirm": confirm,
    }


def test_kline_boundary_keeps_confirmed_history_live(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[
                Candle(
                    60_000,
                    99.9,
                    100.2,
                    99.8,
                    100.0,
                    10,
                    1000,
                    confirmed=False,
                ),
            ],
        )

        engine._apply_kline(
            session,
            {
                "data": [
                    _kline_message_row(
                        60_000,
                        100.1,
                        confirm=True,
                    ),
                    _kline_message_row(
                        120_000,
                        100.3,
                        confirm=False,
                    ),
                ],
            },
        )

        assert [row.start_ms for row in session.candles] == [
            60_000,
            120_000,
        ]
        assert session.candles[0].confirmed is True
        assert session.candles[1].confirmed is False
        assert session.last_price == pytest.approx(100.3)
    finally:
        close_rest(engine)


def test_new_kline_bucket_auto_confirms_previous_forming_minute(
    tmp_path,
) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[
                Candle(
                    60_000,
                    99.9,
                    100.2,
                    99.8,
                    100.0,
                    10,
                    1000,
                    confirmed=False,
                ),
            ],
        )

        engine._apply_kline(
            session,
            {
                "data": [
                    _kline_message_row(
                        120_000,
                        100.3,
                        confirm=False,
                    ),
                ],
            },
        )

        assert session.candles[0].confirmed is True
        assert session.candles[1].confirmed is False
    finally:
        close_rest(engine)


def test_confirmed_kline_cannot_be_downgraded_by_later_live_update(
    tmp_path,
) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[
                Candle(
                    60_000,
                    99.9,
                    100.2,
                    99.8,
                    100.1,
                    10,
                    1000,
                    confirmed=True,
                ),
            ],
        )

        engine._apply_kline(
            session,
            {
                "data": [
                    _kline_message_row(
                        60_000,
                        100.15,
                        confirm=False,
                    ),
                ],
            },
        )

        assert len(session.candles) == 1
        assert session.candles[0].confirmed is True
        assert session.candles[0].close == pytest.approx(100.15)
    finally:
        close_rest(engine)


def test_arbiter_refuses_trade_when_confirmed_1m_history_is_stale(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        confirmed_candle_stale_seconds=150,
    )
    try:
        engine.running = True
        now = time()
        stale_start = int((now - 600) // 60) * 60_000
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[
                Candle(
                    stale_start,
                    100,
                    101,
                    99,
                    100,
                    1,
                    100,
                    confirmed=True,
                ),
            ],
            orderbook=book(),
            last_price=100,
            last_market_at=now,
            last_book_at=now,
            book_synced=True,
        )
        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["would otherwise trade"],
            confidence=0.9,
            entry=100,
            stop=99.5,
            target=101,
            setup_id="stale-candle-setup",
        )
        engine.sessions = {session.symbol: session}
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
        assert engine.market_health()["ready"] is False
        assert "confirmed 1m candle history is stale" in (
            engine.market_health()["reason"] or ""
        )
    finally:
        close_rest(engine)


@pytest.mark.asyncio
async def test_evaluate_replaces_tradeable_signal_with_stale_candle_wait(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        confirmed_candle_stale_seconds=150,
    )
    now = time()
    stale_start = int((now - 600) // 60) * 60_000
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=[
            Candle(
                stale_start,
                100,
                101,
                99,
                100,
                1,
                100,
                confirmed=True,
            ),
        ],
        orderbook=book(),
        last_price=100,
    )
    session.decisions["trend_structure"] = StrategyDecision(
        strategy="trend_structure",
        action=Action.LONG,
        reasons=["old signal"],
        entry=100,
        stop=99,
        target=102,
    )

    try:
        await engine._evaluate(session)
        assert session.decisions["trend_structure"].action == Action.WAIT
        assert (
            session.decisions["trend_structure"].details["state"]
            == "stale_candle"
        )
    finally:
        await engine.rest.close()



def test_strategy_startup_flags_can_isolate_trend_only(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        trend_structure_enabled=True,
        weak_level_rejection_enabled=False,
        density_enabled=False,
        breakout_enabled=False,
    )
    try:
        assert engine.strategy_enabled == {
            "trend_structure": True,
            "weak_level_rejection": False,
            "orderbook_density": False,
            "level_breakout": False,
        }
    finally:
        close_rest(engine)



def test_strategy_stats_count_unique_setup_once_across_updates(
    tmp_path,
) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            trend=Trend.UP,
        )
        first = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["first update"],
            confidence=0.7,
            entry=100.0,
            stop=99.5,
            target=101.0,
            setup_id="trend-episode-1",
        )
        second = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["same setup repriced"],
            confidence=0.75,
            entry=100.05,
            stop=99.5,
            target=101.0,
            setup_id="trend-episode-1",
        )

        engine._record_decision_if_changed(session, first)
        engine._record_decision_if_changed(session, second)
        engine._risk_reject_if_changed(
            session,
            second,
            "reason-one",
        )
        engine._risk_reject_if_changed(
            session,
            second,
            "reason-two",
        )

        stats = engine.strategy_stats["trend_structure"]
        assert stats["tradeableSignals"] == 2
        assert stats["uniqueTradeableSetups"] == 1
        assert stats["riskRejects"] == 2
        assert stats["uniqueRiskRejectedSetups"] == 1
        assert stats["decisionUpdates"] == 2
    finally:
        close_rest(engine)


def test_engaged_setup_uses_fast_evaluation_cadence(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        evaluation_idle_interval_seconds=0.8,
        evaluation_engaged_interval_seconds=0.2,
    )
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
        )
        assert engine._evaluation_interval_seconds(
            session
        ) == pytest.approx(0.8)

        session.decisions["trend_structure"] = StrategyDecision(
            strategy="trend_structure",
            action=Action.WAIT,
            reasons=["armed"],
            confidence=0.2,
            details={"state": "armed"},
        )

        assert engine._session_engaged(session)
        assert engine._evaluation_interval_seconds(
            session
        ) == pytest.approx(0.2)
    finally:
        close_rest(engine)


def test_engaged_evaluation_never_becomes_slower_than_idle(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        evaluation_idle_interval_seconds=0.4,
        evaluation_engaged_interval_seconds=0.9,
    )
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            decisions={
                "level_breakout": StrategyDecision(
                    strategy="level_breakout",
                    action=Action.WAIT,
                    reasons=["armed"],
                    details={"state": "armed"},
                )
            },
        )
        assert engine._evaluation_interval_seconds(
            session
        ) == pytest.approx(0.4)
    finally:
        close_rest(engine)


def test_strategy_state_transition_records_dwell_and_arm_context(
    tmp_path,
) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            trend=Trend.UP,
        )
        armed = StrategyDecision(
            strategy="trend_structure",
            action=Action.WAIT,
            reasons=["armed"],
            watched_level=100.0,
            details={
                "state": "armed",
                "opportunityArm": {
                    "observedAtMs": 100_000,
                    "price": 100.0,
                    "source": "trendline_live_test_armed",
                },
            },
        )
        engine._record_decision_if_changed(
            session,
            armed,
        )

        reclaim = StrategyDecision(
            strategy="trend_structure",
            action=Action.WAIT,
            reasons=["reclaim"],
            watched_level=100.0,
            details={
                "state": "reclaim",
                "opportunityArm": armed.details[
                    "opportunityArm"
                ],
            },
        )
        engine._record_decision_if_changed(
            session,
            reclaim,
        )

        transitions = [
            row
            for row in engine.events
            if row["event"] == "strategy_state_transition"
        ]
        assert len(transitions) == 2
        reclaim_event = next(
            row
            for row in transitions
            if row["payload"]["toState"] == "reclaim"
        )
        payload = reclaim_event["payload"]
        assert payload["fromState"] == "armed"
        assert payload["previousStateDurationSeconds"] is not None
        assert payload["previousStateDurationSeconds"] >= 0.0
        assert (
            payload["opportunityArm"]["source"]
            == "trendline_live_test_armed"
        )
        assert reclaim.details["stateTiming"]["fromState"] == "armed"
        assert reclaim.details["stateTiming"]["toState"] == "reclaim"
    finally:
        close_rest(engine)



def test_arbiter_adds_only_to_matching_staged_probe(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        max_leverage=10,
        max_position_leverage=5,
        max_total_risk_fraction=0.10,
        partial_take_enabled=False,
        passive_entry_enabled=False,
    )
    try:
        engine.running = True
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=OrderBook(
                bids=[(99.99, 1000)],
                asks=[(100.00, 1000)],
            ),
            last_price=100.0,
            last_market_at=now,
            last_book_at=now,
            book_synced=True,
        )
        probe = TradePlan(
            symbol="AAAUSDT",
            strategy="level_breakout",
            side=Side.LONG,
            setup_entry=100.0,
            market_entry=100.0,
            stop=99.5,
            target=101.0,
            notional=100.0,
            leverage=0.1,
            max_loss_usd=0.5,
            expected_gross_profit=1.0,
            estimated_costs=0.0,
            expected_net_profit=1.0,
            expected_net_loss=0.5,
            net_reward_risk=2.0,
            entry_drift_pct=0.0,
            setup_id="stage15:test",
            strategy_details={
                "stagedEntry": {
                    "phase": "probe",
                    "riskFraction": 0.35,
                }
            },
        )
        engine.broker.open(probe, session.orderbook)

        session.decisions["level_breakout"] = StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["confirmation add"],
            confidence=0.9,
            watched_level=100.0,
            entry=100.0,
            stop=99.5,
            target=101.0,
            setup_id=probe.setup_id,
            details={
                "state": "impulse",
                "stagedEntry": {
                    "phase": "add",
                    "riskFraction": 0.65,
                },
                "opportunityFreshness": {
                    "classification": "fresh",
                },
                "flowAlignment": {
                    "classification": "strongly_aligned",
                },
                "liquidityAlignment": {
                    "classification": "supportive",
                },
            },
        )
        engine.sessions = {session.symbol: session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]

        before = engine.broker.positions["AAAUSDT"].notional
        engine._arbitrate_once()

        pos = engine.broker.positions["AAAUSDT"]
        assert pos.notional > before
        assert len(pos.entry_legs) == 2
        assert pos.entry_legs[-1]["phase"] == "add"
        assert engine.strategy_stats[
            "level_breakout"
        ]["positionAdds"] == 1
        assert any(
            event["event"] == "position_added"
            for event in engine.events
        )
    finally:
        close_rest(engine)


def test_arbiter_does_not_treat_orphaned_add_as_new_entry(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        partial_take_enabled=False,
    )
    try:
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
        session.decisions["level_breakout"] = StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["orphaned add"],
            entry=100,
            stop=99.5,
            target=101,
            setup_id="missing-probe",
            details={
                "state": "impulse",
                "stagedEntry": {
                    "phase": "add",
                    "riskFraction": 0.65,
                },
            },
        )
        engine.sessions = {session.symbol: session}
        engine.candidates = [
            Candidate(
                "AAAUSDT",
                200_000_000,
                0,
                100,
                activity_rank=1,
            )
        ]

        engine._arbitrate_once()

        assert "AAAUSDT" not in engine.broker.positions
    finally:
        close_rest(engine)



def test_pending_maker_entry_is_cancelled_when_setup_invalidates(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        passive_entry_enabled=True,
    )
    try:
        pending_plan = plan("AAAUSDT")
        pending_plan.strategy = "weak_level_rejection"
        pending_plan.entry_mode = "maker_limit"
        pending_plan.setup_id = "reject:g1"
        engine.broker.place_pending(pending_plan)

        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100.0,
        )
        session.decisions["weak_level_rejection"] = (
            StrategyDecision(
                strategy="weak_level_rejection",
                action=Action.WAIT,
                reasons=["failed break no longer valid"],
                details={"state": "search"},
            )
        )

        engine._validate_pending_entry(session)

        assert "AAAUSDT" not in engine.broker.pending_entries
        cancelled = next(
            row
            for row in engine.events
            if row["event"] == "entry_cancelled"
        )
        assert cancelled["payload"]["reason"] == (
            "setup_invalidated:setup_no_longer_tradeable"
        )
    finally:
        close_rest(engine)


def test_pending_maker_entry_survives_same_fresh_setup(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        passive_entry_enabled=True,
    )
    try:
        pending_plan = plan("AAAUSDT")
        pending_plan.strategy = "weak_level_rejection"
        pending_plan.entry_mode = "maker_limit"
        pending_plan.setup_id = "reject:g1"
        engine.broker.place_pending(pending_plan)

        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100.0,
        )
        session.decisions["weak_level_rejection"] = (
            StrategyDecision(
                strategy="weak_level_rejection",
                action=Action.LONG,
                reasons=["same rejection remains valid"],
                entry=100.0,
                stop=99.5,
                target=101.0,
                setup_id="reject:g1",
                details={
                    "state": "reject",
                    "opportunityFreshness": {
                        "classification": "fresh",
                    },
                    "entryContextAssessment": {
                        "allowed": True,
                    },
                },
            )
        )

        engine._validate_pending_entry(session)

        assert "AAAUSDT" in engine.broker.pending_entries
        assert not any(
            row["event"] == "entry_cancelled"
            for row in engine.events
        )
    finally:
        close_rest(engine)


def test_pending_maker_entry_cancels_when_freshness_turns_late(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        passive_entry_enabled=True,
    )
    try:
        pending_plan = plan("AAAUSDT")
        pending_plan.strategy = "weak_level_rejection"
        pending_plan.entry_mode = "maker_limit"
        pending_plan.setup_id = "reject:g1"
        engine.broker.place_pending(pending_plan)

        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100.0,
        )
        session.decisions["weak_level_rejection"] = (
            StrategyDecision(
                strategy="weak_level_rejection",
                action=Action.LONG,
                reasons=["same setup but edge aged"],
                entry=100.0,
                stop=99.5,
                target=101.0,
                setup_id="reject:g1",
                details={
                    "state": "reject",
                    "opportunityFreshness": {
                        "classification": "late",
                    },
                },
            )
        )

        engine._validate_pending_entry(session)

        assert "AAAUSDT" not in engine.broker.pending_entries
        assert any(
            row["event"] == "entry_cancelled"
            and row["payload"]["reason"]
            == "setup_invalidated:setup_freshness_late"
            for row in engine.events
        )
    finally:
        close_rest(engine)



def test_strategy_invalidation_no_longer_waits_five_seconds(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        strategy_invalidation_grace_seconds=0.5,
        partial_take_enabled=False,
    )

    class ImmediateInvalidation:
        key = "orderbook_density"
        label = "fixture"

        def manage_position(self, **kwargs):
            return "fixture_structural_invalidation"

        def reset(self, symbol: str) -> None:
            return None

        def mark_opened(self, symbol: str, decision) -> None:
            return None

    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(),
            last_price=100.0,
        )
        engine.sessions[session.symbol] = session
        engine.strategies["orderbook_density"] = ImmediateInvalidation()  # type: ignore[assignment]
        opened = engine.broker.open(
            plan(session.symbol),
            session.orderbook,
        )
        opened.opened_at = time() - 1.0

        engine._maybe_strategy_invalidation(session)

        assert session.symbol not in engine.broker.positions
        assert engine.broker.closed_trades[-1]["reason"] == (
            "fixture_structural_invalidation"
        )
    finally:
        close_rest(engine)



def test_execution_uses_specific_public_trade_price_for_maker_fill(
    tmp_path,
) -> None:
    engine = make_engine(
        tmp_path,
        passive_entry_enabled=True,
        maker_fill_confirmation_bps=0.0,
    )
    try:
        pending_plan = plan("AAAUSDT")
        pending_plan.strategy = "weak_level_rejection"
        pending_plan.entry_mode = "maker_limit"
        pending_plan.market_entry = 99.99
        pending_plan.setup_id = "reject:g1"
        engine.broker.place_pending(pending_plan)

        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[candle()],
            orderbook=book(99.99, 100.01),
            # Final batch price can be back above the resting bid.
            last_price=100.10,
        )
        engine.sessions[session.symbol] = session

        engine._mark_execution_from_market(
            session,
            trade_ts_ms=1_001,
            trade_price=99.98,
        )

        assert "AAAUSDT" not in engine.broker.pending_entries
        assert "AAAUSDT" in engine.broker.positions
        assert engine.broker.positions[
            "AAAUSDT"
        ].entry == pytest.approx(99.99)
    finally:
        close_rest(engine)
