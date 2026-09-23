import asyncio

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision, TradeTick
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy.flow_context import build_multi_horizon_flow_context


def make_engine(tmp_path) -> TradingEngine:
    return TradingEngine(
        Settings(
            session_dir=str(tmp_path),
            confirmed_candle_stale_seconds=0,
        )
    )


def close_engine(engine: TradingEngine) -> None:
    asyncio.run(engine.rest.close())


def test_book_flow_snapshot_reports_event_counts_and_age() -> None:
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        orderbook=OrderBook(
            bids=[(100.0, 10)],
            asks=[(100.1, 10)],
        ),
    )
    session.record_book_flow(97_000, 1_000.0)
    session.record_book_flow(99_000, -250.0)

    snapshot = session.book_flow_snapshot(100_000)

    assert snapshot["eventCount5s"] == 2
    assert snapshot["eventCount15s"] == 2
    assert snapshot["eventCount60s"] == 2
    assert snapshot["bestLevelOfiUsd5s"] == 750.0
    assert snapshot["latestEventAgeMs"] == 1_000


def test_engine_attaches_side_specific_flow_alignment_without_changing_action(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(symbol="AAAUSDT")
        session.flow_context = build_multi_horizon_flow_context(
            {
                "imbalance5s": -0.8,
                "imbalance15s": -0.6,
                "imbalance60s": -0.5,
                "cvd5s": -8_000,
                "cvd15s": -18_000,
                "cvd60s": -40_000,
                "notional5s": 10_000,
                "notional15s": 30_000,
                "notional60s": 80_000,
                "tradeCount5s": 20,
                "tradeCount15s": 50,
                "tradeCount60s": 120,
            },
            {
                "bestLevelOfiUsd5s": -5_000,
                "bestLevelOfiUsd15s": -8_000,
                "bestLevelOfiUsd60s": -12_000,
                "normalizedOfi5s": -0.05,
                "normalizedOfi15s": -0.08,
                "normalizedOfi60s": -0.12,
                "eventCount5s": 5,
                "eventCount15s": 12,
                "eventCount60s": 30,
            },
            observed_at_ms=100_000,
        )
        decision = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["ready"],
            entry=100.0,
            stop=99.5,
            target=101.0,
            details={"state": "continuation"},
        )

        engine._annotate_flow_context(session, decision)

        assert decision.action == Action.LONG
        assert (
            decision.details["flowAlignment"]["classification"]
            == "opposed"
        )
        assert (
            decision.details["multiHorizonFlow"]["dominantDirection"]
            == "down"
        )
    finally:
        close_engine(engine)


def test_flow_context_uses_current_trade_and_book_windows() -> None:
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        orderbook=OrderBook(
            bids=[(100.0, 100)],
            asks=[(100.1, 100)],
        ),
    )
    now = 100_000
    session.trades.extend(
        [
            TradeTick(now - 50_000, 100.0, 1, "Sell"),
            TradeTick(now - 12_000, 100.0, 2, "Sell"),
            TradeTick(now - 3_000, 100.0, 5, "Buy"),
            TradeTick(now - 2_000, 100.0, 5, "Buy"),
            TradeTick(now - 1_000, 100.0, 5, "Buy"),
        ]
    )
    session.record_book_flow(now - 4_000, 2_000)
    session.record_book_flow(now - 2_000, 3_000)

    from scalp_bot.strategy.common import compute_trade_flow

    context = build_multi_horizon_flow_context(
        compute_trade_flow(list(session.trades), now),
        session.book_flow_snapshot(now),
        observed_at_ms=now,
    )

    assert context.horizons[5].trade_count == 3
    assert context.horizons[15].trade_count == 4
    assert context.horizons[60].trade_count == 5
    assert context.horizons[5].ofi_event_count == 2
