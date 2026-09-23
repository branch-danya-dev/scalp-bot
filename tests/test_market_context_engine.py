import time
import pytest
import asyncio

from scalp_bot.config import Settings
from scalp_bot.domain import Action, Candle, OrderBook, StrategyDecision, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy.market_context import (
    build_execution_context,
    build_structure_context,
)
from scalp_bot.strategy.regime import LocalRegime
from scalp_bot.strategy.structure import (
    MarketStructure,
    StructuralLevel,
    TrendLine,
)


def candles_from_closes(
    closes: list[float],
    *,
    interval_ms: int,
    range_pad: float = 0.025,
) -> list[Candle]:
    rows: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous if index else close
        rows.append(
            Candle(
                start_ms=index * interval_ms,
                open=open_price,
                high=max(open_price, close) + range_pad,
                low=min(open_price, close) - range_pad,
                close=close,
                volume=100.0,
                turnover=close * 100.0,
                confirmed=True,
            )
        )
        previous = close
    return rows


def flat_rows(count: int, interval_ms: int) -> list[Candle]:
    closes = [
        100.0 + (0.01 if index % 2 else -0.01)
        for index in range(count)
    ]
    return candles_from_closes(
        closes,
        interval_ms=interval_ms,
        range_pad=0.02,
    )


def make_engine(tmp_path) -> TradingEngine:
    return TradingEngine(
        Settings(
            session_dir=str(tmp_path),
            confirmed_candle_stale_seconds=0,
        )
    )


def close_engine(engine: TradingEngine) -> None:
    asyncio.run(engine.rest.close())


def test_engine_tracks_local_impulse_separately_from_legacy_htf_trend(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        baseline = [
            100.0 + ((index % 4) - 1.5) * 0.01
            for index in range(40)
        ]
        recent = [
            100.05,
            100.17,
            100.30,
            100.44,
            100.59,
            100.75,
        ]
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=candles_from_closes(
                [*baseline, *recent],
                interval_ms=60_000,
            ),
            context_5m=flat_rows(40, 5 * 60_000),
            context_15m=flat_rows(40, 15 * 60_000),
            context_1h=flat_rows(40, 60 * 60_000),
        )
        engine.sessions[session.symbol] = session

        engine._refresh_market_context(
            session,
            closed_1m=session.candles,
            closed_5m=session.context_5m,
            closed_15m=session.context_15m,
            closed_1h=session.context_1h,
        )

        assert session.trend == Trend.FLAT
        assert session.local_regime is not None
        assert session.local_regime.regime == LocalRegime.BULLISH_IMPULSE

        public = session.market_context_public()
        assert public["legacyTrend"] == "flat"
        assert public["localRegime"]["regime"] == "bullish_impulse"
        assert public["htfBias"]["bias"] == "neutral"
    finally:
        close_engine(engine)


def test_market_frame_exposes_market_context_without_changing_legacy_trend(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=flat_rows(40, 60_000),
            context_5m=flat_rows(40, 5 * 60_000),
            context_15m=flat_rows(40, 15 * 60_000),
            context_1h=flat_rows(40, 60 * 60_000),
        )
        engine.sessions[session.symbol] = session
        engine._refresh_market_context(
            session,
            closed_1m=session.candles,
            closed_5m=session.context_5m,
            closed_15m=session.context_15m,
            closed_1h=session.context_1h,
        )

        frame = session.frame(5, None)

        assert frame["trend"] == "flat"
        assert frame["marketContext"]["legacyTrend"] == "flat"
        assert frame["marketContext"]["htfBias"]["bias"] == "neutral"
        assert frame["marketContext"]["localRegime"]["regime"] in {
            "range",
            "unclear",
        }
    finally:
        close_engine(engine)



def test_structure_context_exposes_nearest_support_resistance_and_trendlines() -> None:
    structure = MarketStructure(
        levels=[
            StructuralLevel(
                kind="support",
                low=99.0,
                high=99.2,
                touches=3,
                timeframe="5m",
                score=0.8,
            ),
            StructuralLevel(
                kind="resistance",
                low=100.8,
                high=101.0,
                touches=4,
                timeframe="15m",
                score=0.9,
            ),
        ],
        trendlines=[
            TrendLine(
                kind="support",
                timeframe="5m",
                start_ms=0,
                end_ms=60_000,
                start_price=98.0,
                end_price=99.0,
                current_price=99.4,
                touches=3,
                score=0.8,
                slope_per_bar=0.1,
            ),
        ],
    )

    context = build_structure_context(structure, 100.0)

    assert context is not None
    assert context.nearest_support is not None
    assert context.nearest_resistance is not None
    assert context.nearest_support.center == 99.1
    assert context.nearest_resistance.center == 100.9
    assert context.support_distance_pct == pytest.approx(0.009)
    assert context.resistance_distance_pct == pytest.approx(0.009)
    assert context.support_trendline is not None


def test_execution_context_summarizes_book_health_and_depth() -> None:
    book = OrderBook(
        bids=[(100.0, 10.0), (99.9, 5.0)],
        asks=[(100.1, 8.0), (100.2, 4.0)],
    )

    context = build_execution_context(
        book=book,
        book_fresh=True,
        book_synced=True,
        book_age_seconds=0.2,
        candle_fresh=True,
        candle_age_seconds=1.0,
        trade_buffer_seconds=42.0,
    )

    assert context.ready is True
    assert context.top5_bid_notional_usd == pytest.approx(1499.5)
    assert context.top5_ask_notional_usd == pytest.approx(1201.6)
    assert context.top5_depth_usd == pytest.approx(2701.1)


class CaptureStrategy:
    def __init__(self, key: str) -> None:
        self.key = key
        self.label = key
        self.seen_contexts = []
        self.seen_candle_counts = []

    def evaluate(
        self,
        candles,
        book,
        trend,
        *,
        symbol="",
        trades=None,
        structure=None,
        market_context=None,
        observed_at_ms=None,
    ):
        self.seen_contexts.append(market_context)
        self.seen_candle_counts.append(len(candles))
        return StrategyDecision(
            strategy=self.key,
            action=Action.WAIT,
            reasons=["capture"],
            details={"state": "search"},
        )

    def reset(self, symbol: str) -> None:
        return None

    def manage_position(self, **kwargs):
        return None


def test_tradeable_playbooks_receive_same_canonical_market_context(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        strategies = {
            key: CaptureStrategy(key)
            for key in (
                "trend_structure",
                "weak_level_rejection",
                "level_breakout",
            )
        }
        engine.strategies = strategies
        engine.strategy_enabled = {
            key: True
            for key in strategies
        }

        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=flat_rows(80, 60_000),
            context_5m=flat_rows(80, 5 * 60_000),
            context_15m=flat_rows(80, 15 * 60_000),
            context_1h=flat_rows(80, 60 * 60_000),
            orderbook=OrderBook(
                bids=[(99.99, 100.0)],
                asks=[(100.01, 100.0)],
            ),
            last_price=100.0,
            last_market_at=time.time(),
            last_book_at=time.time(),
            book_synced=True,
            confirmed_candle_stale_after_seconds=0,
        )
        engine.sessions[session.symbol] = session

        asyncio.run(engine._evaluate(session))

        assert session.market_context is not None
        seen = [
            strategy.seen_contexts[0]
            for strategy in strategies.values()
        ]
        assert all(context is session.market_context for context in seen)
        public = session.market_context.public()
        assert public["schemaVersion"] == 1
        assert public["structureContext"] is not None
        assert public["executionContext"]["bookFresh"] is True
        assert public["flowContext"] is not None
    finally:
        close_engine(engine)


def test_engine_exposes_forming_candle_without_passing_it_as_confirmed_structure(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        strategy = CaptureStrategy("trend_structure")
        engine.strategies = {strategy.key: strategy}
        engine.strategy_enabled = {strategy.key: True}

        closed = flat_rows(80, 60_000)
        forming = Candle(
            start_ms=80 * 60_000,
            open=100.0,
            high=100.30,
            low=99.95,
            close=100.25,
            volume=150.0,
            turnover=150.0 * 100.25,
            confirmed=False,
        )
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=[*closed, forming],
            context_5m=flat_rows(80, 5 * 60_000),
            context_15m=flat_rows(80, 15 * 60_000),
            context_1h=flat_rows(80, 60 * 60_000),
            orderbook=OrderBook(
                bids=[(100.24, 100.0)],
                asks=[(100.26, 100.0)],
            ),
            last_price=100.25,
            last_market_at=time.time(),
            last_book_at=time.time(),
            book_synced=True,
            confirmed_candle_stale_after_seconds=0,
        )
        engine.sessions[session.symbol] = session

        asyncio.run(engine._evaluate(session))

        assert strategy.seen_candle_counts == [80]
        assert strategy.seen_contexts[0] is session.market_context
        assert session.market_context is not None
        assert session.market_context.forming_candle is not None
        assert session.market_context.forming_candle.close == pytest.approx(
            100.25
        )
        assert session.market_context.public()["formingCandle"]["close"] == pytest.approx(
            100.25
        )
    finally:
        close_engine(engine)


def test_decision_context_binds_entry_freshness_to_same_market_snapshot(tmp_path) -> None:
    engine = make_engine(tmp_path)
    try:
        session = ActiveSymbolSession(
            symbol="AAAUSDT",
            candles=flat_rows(80, 60_000),
            orderbook=OrderBook(
                bids=[(99.99, 100.0)],
                asks=[(100.01, 100.0)],
            ),
            last_price=100.0,
            last_market_at=time.time(),
            last_book_at=time.time(),
            book_synced=True,
            confirmed_candle_stale_after_seconds=0,
        )
        engine.sessions[session.symbol] = session
        engine._refresh_market_context(
            session,
            closed_1m=session.candles,
            closed_5m=[],
            closed_15m=[],
            closed_1h=[],
            commit=False,
        )
        engine._commit_market_context(
            session,
            observed_at_ms=123_000,
            emit=False,
        )
        decision = StrategyDecision(
            strategy="trend_structure",
            action=Action.LONG,
            reasons=["ready"],
            entry=100.0,
            stop=99.5,
            target=101.0,
            details={
                "state": "continuation",
                "entryFreshness": {
                    "classification": "late",
                    "moveSpentRatio": 0.62,
                },
            },
        )

        engine._annotate_decision_context(session, decision)

        overlay = decision.details["decisionContext"]
        assert overlay["marketObservedAtMs"] == 123_000
        assert overlay["marketContextFingerprint"] == list(
            session.market_context.fingerprint()
        )
        assert overlay["entryFreshness"]["classification"] == "late"
        assert overlay["executionReady"] is True
    finally:
        close_engine(engine)



def test_live_fast_path_reuses_confirmed_candle_analysis(
    tmp_path,
    monkeypatch,
) -> None:
    import scalp_bot.engine as engine_module

    engine = make_engine(tmp_path)
    calls = {"structure": 0}
    original_build = engine_module.build_market_structure

    def counted_build(*args, **kwargs):
        calls["structure"] += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        engine_module,
        "build_market_structure",
        counted_build,
    )
    strategy = CaptureStrategy("trend_structure")
    engine.strategies = {strategy.key: strategy}
    engine.strategy_enabled = {strategy.key: True}

    closed = flat_rows(80, 60_000)
    forming = Candle(
        start_ms=80 * 60_000,
        open=100.0,
        high=100.10,
        low=99.95,
        close=100.05,
        volume=50,
        turnover=5_002.5,
        confirmed=False,
    )
    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=[*closed, forming],
        context_5m=flat_rows(80, 5 * 60_000),
        context_15m=flat_rows(80, 15 * 60_000),
        context_1h=flat_rows(80, 60 * 60_000),
        orderbook=OrderBook(
            bids=[(100.04, 100)],
            asks=[(100.06, 100)],
        ),
        last_price=100.05,
        last_market_at=time.time(),
        last_book_at=time.time(),
        book_synced=True,
        confirmed_candle_stale_after_seconds=0,
    )
    engine.sessions[session.symbol] = session

    try:
        asyncio.run(engine._evaluate(session))
        first_structure = session.structure
        assert calls["structure"] == 1
        assert session.static_analysis_rebuilds == 1
        assert session.live_fast_path_reuses == 0
        assert session.last_analysis_mode == "static_rebuild"

        # Only live market evidence changes; confirmed-candle inputs do not.
        session.orderbook = OrderBook(
            bids=[(100.08, 100)],
            asks=[(100.10, 100)],
        )
        session.last_price = 100.09
        session.candles[-1].close = 100.09
        session.candles[-1].high = 100.12

        asyncio.run(engine._evaluate(session))

        assert calls["structure"] == 1
        assert session.structure is first_structure
        assert session.static_analysis_rebuilds == 1
        assert session.live_fast_path_reuses == 1
        assert session.last_analysis_mode == "live_fast_path"
        assert (
            strategy.seen_contexts[-1].forming_candle.close
            == pytest.approx(100.09)
        )
    finally:
        close_engine(engine)


def test_new_confirmed_candle_invalidates_live_fast_path_cache(
    tmp_path,
    monkeypatch,
) -> None:
    import scalp_bot.engine as engine_module

    engine = make_engine(tmp_path)
    calls = {"structure": 0}
    original_build = engine_module.build_market_structure

    def counted_build(*args, **kwargs):
        calls["structure"] += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        engine_module,
        "build_market_structure",
        counted_build,
    )
    strategy = CaptureStrategy("trend_structure")
    engine.strategies = {strategy.key: strategy}
    engine.strategy_enabled = {strategy.key: True}

    session = ActiveSymbolSession(
        symbol="AAAUSDT",
        candles=flat_rows(80, 60_000),
        context_5m=flat_rows(80, 5 * 60_000),
        context_15m=flat_rows(80, 15 * 60_000),
        context_1h=flat_rows(80, 60 * 60_000),
        orderbook=OrderBook(
            bids=[(99.99, 100)],
            asks=[(100.01, 100)],
        ),
        last_price=100.0,
        last_market_at=time.time(),
        last_book_at=time.time(),
        book_synced=True,
        confirmed_candle_stale_after_seconds=0,
    )
    engine.sessions[session.symbol] = session

    try:
        asyncio.run(engine._evaluate(session))
        first_key = session.static_analysis_key
        assert calls["structure"] == 1

        session.candles.append(
            Candle(
                start_ms=80 * 60_000,
                open=100.0,
                high=100.20,
                low=99.95,
                close=100.15,
                volume=120,
                turnover=12_018,
                confirmed=True,
            )
        )
        session.last_price = 100.15

        asyncio.run(engine._evaluate(session))

        assert calls["structure"] == 2
        assert session.static_analysis_key != first_key
        assert session.static_analysis_rebuilds == 2
        assert session.last_analysis_mode == "static_rebuild"
    finally:
        close_engine(engine)
