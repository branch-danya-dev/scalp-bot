import pytest

from scalp_bot.domain import Candle, Trend
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
