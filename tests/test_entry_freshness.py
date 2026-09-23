import asyncio

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy.freshness import (
    EntryFreshnessClass,
    classify_entry_freshness,
)


def tradeable(
    strategy: str,
    *,
    action: Action,
    entry: float,
    stop: float,
    target: float,
    state: str,
    watched_level: float = 100.0,
    details: dict | None = None,
) -> StrategyDecision:
    payload = {"state": state, **(details or {})}
    return StrategyDecision(
        strategy=strategy,
        action=action,
        reasons=["ready"],
        confidence=0.8,
        watched_level=watched_level,
        entry=entry,
        stop=stop,
        target=target,
        details=payload,
    )


def test_entry_freshness_classifies_move_spent_ratio() -> None:
    decision = tradeable(
        "level_breakout",
        action=Action.LONG,
        entry=100.60,
        stop=99.80,
        target=102.0,
        state="impulse",
        details={"expectedImpulsePct": 0.01},
    )

    freshness = classify_entry_freshness(
        decision,
        trigger_price=100.0,
        trigger_ts=100.0,
        current_price=100.60,
        observed_ts=106.0,
        source="break_state",
    )

    assert freshness.classification == EntryFreshnessClass.LATE
    assert 0.59 <= freshness.move_spent_ratio <= 0.61
    assert freshness.confirmation_age_seconds == 6.0


def test_opportunity_age_can_mark_low_drift_breakout_exhausted() -> None:
    decision = tradeable(
        "level_breakout",
        action=Action.SHORT,
        entry=99.95,
        stop=100.4,
        target=99.0,
        state="impulse",
        details={"expectedImpulsePct": 0.01},
    )

    freshness = classify_entry_freshness(
        decision,
        trigger_price=100.0,
        trigger_ts=100.0,
        current_price=99.95,
        observed_ts=283.8,
        source="pressure_state",
    )

    assert freshness.move_spent_ratio < 0.1
    assert freshness.time_spent_ratio > 1.5
    assert freshness.effective_spent_ratio == freshness.time_spent_ratio
    assert freshness.classification == EntryFreshnessClass.EXHAUSTED
    assert any(
        "elapsed opportunity age" in reason
        for reason in freshness.reasons
    )


def test_entry_freshness_marks_exhausted_setup() -> None:
    decision = tradeable(
        "level_breakout",
        action=Action.LONG,
        entry=100.90,
        stop=99.80,
        target=102.0,
        state="impulse",
        details={"expectedImpulsePct": 0.01},
    )

    freshness = classify_entry_freshness(
        decision,
        trigger_price=100.0,
        trigger_ts=100.0,
        current_price=100.90,
        observed_ts=105.0,
        source="break_state",
    )

    assert freshness.classification == EntryFreshnessClass.EXHAUSTED
    assert freshness.move_spent_ratio > 0.8


def test_entry_freshness_records_adverse_move_without_calling_it_spent() -> None:
    decision = tradeable(
        "trend_structure",
        action=Action.LONG,
        entry=99.8,
        stop=99.0,
        target=101.0,
        state="continuation",
        details={"expectedImpulsePct": 0.01},
    )

    freshness = classify_entry_freshness(
        decision,
        trigger_price=100.0,
        trigger_ts=100.0,
        current_price=99.8,
        observed_ts=110.0,
        source="reclaim_state",
    )

    assert freshness.signed_move_since_trigger_pct < 0
    assert freshness.move_since_trigger_pct == 0
    assert freshness.move_spent_ratio == 0
    assert any("behind the causal trigger" in row for row in freshness.reasons)


def make_engine(tmp_path) -> TradingEngine:
    return TradingEngine(
        Settings(
            session_dir=str(tmp_path),
            confirmed_candle_stale_seconds=0,
        )
    )


def close_engine(engine: TradingEngine) -> None:
    asyncio.run(engine.rest.close())


def test_engine_preserves_reclaim_anchor_until_tradeable_continuation(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            last_price=100.10,
            orderbook=OrderBook(
                bids=[(100.09, 10)],
                asks=[(100.11, 10)],
            ),
        )
        engine.sessions[session.symbol] = session

        reclaim = StrategyDecision(
            strategy="trend_structure",
            action=Action.WAIT,
            reasons=["reclaim confirmed"],
            watched_level=100.0,
            details={
                "state": "reclaim",
                "trendlineAnchor": ["support", "1m", 1, 100.0, 0.01],
                "reclaimPrice": 100.10,
            },
        )
        engine._annotate_entry_freshness(
            session,
            reclaim,
            observed_at=100.0,
        )

        continuation = tradeable(
            "trend_structure",
            action=Action.LONG,
            entry=100.70,
            stop=99.70,
            target=102.0,
            state="continuation",
            details={
                "trendlineAnchor": ["support", "1m", 1, 100.0, 0.01],
                "reclaimPrice": 100.10,
                "expectedImpulsePct": 0.01,
            },
        )
        engine._annotate_entry_freshness(
            session,
            continuation,
            observed_at=108.0,
        )

        freshness = continuation.details["entryFreshness"]
        assert freshness["source"] == "reclaim_state"
        assert freshness["triggerTs"] == 100.0
        assert freshness["confirmationAgeSeconds"] == 8.0
        assert freshness["classification"] == "late"
        assert continuation.details["armToFireSeconds"] == 8.0
        assert continuation.details["causalTriggerSource"] == "reclaim_state"
    finally:
        close_engine(engine)


def test_breakout_tradeable_without_prior_break_uses_boundary_fallback(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            last_price=100.90,
            orderbook=OrderBook(
                bids=[(100.89, 10)],
                asks=[(100.91, 10)],
            ),
        )
        engine.sessions[session.symbol] = session

        decision = tradeable(
            "level_breakout",
            action=Action.LONG,
            entry=100.90,
            stop=99.80,
            target=102.0,
            state="impulse",
            details={
                "zoneGeneration": ["resistance", "g1", 100.0],
                "acceptanceBoundary": 100.0,
                "expectedImpulsePct": 0.01,
            },
        )
        engine._annotate_entry_freshness(
            session,
            decision,
            observed_at=100.0,
        )

        freshness = decision.details["entryFreshness"]
        assert freshness["source"] == "breakout_boundary"
        assert freshness["triggerTs"] is None
        assert freshness["classification"] == "exhausted"
    finally:
        close_engine(engine)


def test_search_state_resets_old_freshness_anchor(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            last_price=100.0,
            orderbook=OrderBook(
                bids=[(99.99, 10)],
                asks=[(100.01, 10)],
            ),
        )
        engine.sessions[session.symbol] = session

        reclaim = StrategyDecision(
            strategy="trend_structure",
            action=Action.WAIT,
            reasons=["reclaim"],
            watched_level=100.0,
            details={
                "state": "reclaim",
                "trendlineAnchor": ["support", "1m", 1, 100.0, 0.01],
            },
        )
        engine._annotate_entry_freshness(
            session,
            reclaim,
            observed_at=100.0,
        )
        assert "trend_structure" in session.entry_freshness_anchors

        search = StrategyDecision(
            strategy="trend_structure",
            action=Action.WAIT,
            reasons=["reset"],
            details={"state": "search"},
        )
        engine._annotate_entry_freshness(
            session,
            search,
            observed_at=101.0,
        )

        assert "trend_structure" not in session.entry_freshness_anchors
    finally:
        close_engine(engine)



def test_rejection_generation_keeps_reject_anchor_until_reaction(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            last_price=100.05,
            orderbook=OrderBook(
                bids=[(100.04, 10)],
                asks=[(100.06, 10)],
            ),
        )
        engine.sessions[session.symbol] = session

        reject = StrategyDecision(
            strategy="weak_level_rejection",
            action=Action.WAIT,
            reasons=["failed break confirmed"],
            watched_level=100.0,
            details={
                "state": "reject",
                "levelGeneration": "support:g1",
                "zone": {
                    "kind": "support",
                    "low": 99.90,
                    "high": 100.00,
                },
            },
        )
        engine._annotate_entry_freshness(
            session,
            reject,
            observed_at=200.0,
        )

        reaction = tradeable(
            "weak_level_rejection",
            action=Action.LONG,
            entry=100.30,
            stop=99.80,
            target=101.00,
            state="reaction",
            watched_level=100.0,
            details={
                "levelGeneration": "support:g1",
                "zone": {
                    "kind": "support",
                    "low": 99.90,
                    "high": 100.00,
                },
                "expectedImpulsePct": 0.01,
            },
        )
        engine._annotate_entry_freshness(
            session,
            reaction,
            observed_at=205.0,
        )

        freshness = reaction.details["entryFreshness"]
        assert freshness["source"] == "reject_state"
        assert freshness["triggerTs"] == 200.0
        assert freshness["confirmationAgeSeconds"] == 5.0
    finally:
        close_engine(engine)
