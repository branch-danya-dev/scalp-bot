import pytest

from scalp_bot.domain import Candle, TradeTick, Trend
from scalp_bot.strategy.pre_state import build_forming_candle_context


def candle(
    start_ms: int,
    *,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    confirmed: bool,
) -> Candle:
    return Candle(
        start_ms=start_ms,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        turnover=volume * close,
        confirmed=confirmed,
    )


def test_forming_candle_context_uses_live_bar_without_promoting_it_to_structure() -> None:
    closed = [
        candle(
            index * 60_000,
            open=100.0,
            high=100.10,
            low=99.90,
            close=100.0,
            volume=120.0,
            confirmed=True,
        )
        for index in range(20)
    ]
    forming = candle(
        20 * 60_000,
        open=100.0,
        high=100.30,
        low=99.98,
        close=100.25,
        volume=90.0,
        confirmed=False,
    )

    context = build_forming_candle_context(
        forming,
        closed,
        observed_at_ms=20 * 60_000 + 30_000,
    )

    assert context is not None
    assert context.direction == Trend.UP
    assert context.age_seconds == pytest.approx(30.0)
    assert context.progress_ratio == pytest.approx(0.5)
    assert context.body_pct == pytest.approx(0.0025)
    assert context.range_pct == pytest.approx(0.0032)
    assert context.close_position > 0.8
    assert context.volume_pace_ratio == pytest.approx(1.5)
    assert context.range_expansion_ratio == pytest.approx(1.6)
    assert context.velocity_bps_per_second > 0


def test_forming_context_ignores_confirmed_candle() -> None:
    forming = candle(
        60_000,
        open=100.0,
        high=100.2,
        low=99.9,
        close=100.1,
        volume=100.0,
        confirmed=True,
    )

    assert build_forming_candle_context(
        forming,
        [],
        observed_at_ms=90_000,
    ) is None


def test_forming_context_handles_zero_range_bar() -> None:
    forming = candle(
        60_000,
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=0.0,
        confirmed=False,
    )

    context = build_forming_candle_context(
        forming,
        [],
        observed_at_ms=60_100,
    )

    assert context is not None
    assert context.direction == Trend.FLAT
    assert context.close_position == pytest.approx(0.5)
    assert context.body_to_range == 0.0



def test_forming_context_exposes_hybrid_tape_microstructure() -> None:
    start = 20 * 60_000
    forming = candle(
        start,
        open=100.0,
        high=100.20,
        low=99.95,
        close=100.10,
        volume=60.0,
        confirmed=False,
    )
    trades = [
        TradeTick(start + 20_000, 100.02, 1, "Buy"),
        TradeTick(start + 27_000, 100.08, 1, "Buy"),
        TradeTick(start + 29_000, 100.22, 2, "Buy"),
        TradeTick(start + 30_000, 100.18, 1, "Sell"),
    ]

    context = build_forming_candle_context(
        forming,
        [],
        observed_at_ms=start + 30_000,
        recent_trades=trades,
        tape_updates=3,
        last_trade_ts_ms=start + 30_000,
        kline_snapshot_observed_at_ms=start + 25_000,
        price_source="hybrid_tape",
        volume_source="kline_snapshot",
    )

    assert context is not None
    assert context.price_source == "hybrid_tape"
    assert context.volume_source == "kline_snapshot"
    assert context.tape_updates == 3
    assert context.current_minute_trade_count == 4
    assert context.last_trade_age_seconds == pytest.approx(0.0)
    assert context.kline_snapshot_age_seconds == pytest.approx(5.0)
    assert context.micro_move_5s_bps is not None
    assert context.micro_move_5s_bps > 0
    assert context.micro_range_5s_bps is not None
    assert context.micro_range_5s_bps > 0
    assert context.micro_move_15s_bps is not None
    assert context.public()["priceSource"] == "hybrid_tape"
