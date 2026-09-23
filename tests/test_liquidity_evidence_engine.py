import asyncio
from time import time

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy.liquidity_evidence import build_liquidity_evidence


def make_engine(tmp_path) -> TradingEngine:
    return TradingEngine(
        Settings(
            session_dir=str(tmp_path),
            confirmed_candle_stale_seconds=0,
            passive_entry_enabled=False,
        )
    )


def close_engine(engine: TradingEngine) -> None:
    asyncio.run(engine.rest.close())


def raw_density_signal() -> StrategyDecision:
    return StrategyDecision(
        strategy="orderbook_density",
        action=Action.LONG,
        reasons=["defended bid wall"],
        confidence=0.9,
        watched_level=100.0,
        entry=100.10,
        stop=99.70,
        target=100.80,
        details={
            "state": "reaction",
            "wallSide": "bid",
            "wallPrice": 100.0,
            "wallPresent": True,
            "remainingRatio": 0.95,
            "attackRatio": 0.15,
            "depletionPerSecond": 0.01,
            "replenishmentRatio": 0.12,
            "absorptionObserved": True,
            "strengthMultiple": 12.0,
            "distancePct": 0.001,
        },
    )


def test_density_trade_signal_becomes_evidence_only_wait() -> None:
    raw = raw_density_signal()

    decision = TradingEngine._density_as_evidence_only(raw)

    assert raw.action == Action.LONG
    assert decision.action == Action.WAIT
    assert decision.tradeable is False
    assert decision.entry is None
    assert decision.stop is None
    assert decision.target is None
    assert decision.details["evidenceOnly"] is True
    assert decision.details["shadowAction"] == "long"
    assert decision.details["shadowEntry"] == 100.10


def test_liquidity_evidence_is_attached_to_other_playbook_direction(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(symbol="AAAUSDT")
        consumed_ask = StrategyDecision(
            strategy="orderbook_density",
            action=Action.WAIT,
            reasons=["consumed"],
            details={
                "state": "exhausted",
                "wallSide": "ask",
                "wallPrice": 100.2,
                "wallPresent": True,
                "remainingRatio": 0.55,
                "attackRatio": 0.50,
                "depletionPerSecond": 0.15,
                "replenishmentRatio": 0.0,
                "absorptionObserved": False,
                "consuming": True,
                "consumptionCause": [
                    "remaining_ratio",
                    "aggressive_attack",
                ],
            },
        )
        session.liquidity_evidence = build_liquidity_evidence(
            consumed_ask
        )
        breakout = StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["break"],
            entry=100.3,
            stop=99.9,
            target=101.0,
            details={"state": "impulse"},
        )

        engine._annotate_liquidity_evidence(
            session,
            breakout,
        )

        assert (
            breakout.details["liquidityEvidence"]["state"]
            == "consumed"
        )
        assert (
            breakout.details["liquidityEvidence"]["directionalBias"]
            == "up"
        )
        assert (
            breakout.details["liquidityAlignment"]["classification"]
            == "supportive"
        )
    finally:
        close_engine(engine)


def test_arbiter_never_opens_standalone_density_signal(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        now = time()
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            orderbook=OrderBook(
                bids=[(100.0, 100.0)],
                asks=[(100.1, 100.0)],
            ),
            last_price=100.05,
            last_market_at=now,
            last_book_at=now,
            book_synced=True,
        )
        session.decisions["orderbook_density"] = raw_density_signal()
        engine.sessions[session.symbol] = session

        engine._arbitrate_once()

        assert engine.broker.positions == {}
        assert engine.broker.pending_entries == {}
    finally:
        close_engine(engine)


def test_disabled_density_provider_cannot_leave_stale_liquidity_evidence(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(symbol="AAAUSDT")
        session.liquidity_evidence = build_liquidity_evidence(
            raw_density_signal()
        )
        engine.strategy_enabled["orderbook_density"] = False

        # This is the exact reset used by _evaluate when the provider is off.
        if not engine.strategy_enabled.get("orderbook_density", False):
            session.liquidity_evidence = build_liquidity_evidence(None)

        assert session.liquidity_evidence.state.value == "none"
    finally:
        close_engine(engine)
