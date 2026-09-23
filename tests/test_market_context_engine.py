import asyncio

from scalp_bot.config import Settings
from scalp_bot.domain import Candle, Trend
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.strategy.regime import LocalRegime


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
