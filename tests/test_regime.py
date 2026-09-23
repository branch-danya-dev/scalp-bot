from scalp_bot.domain import Candle, Trend
from scalp_bot.strategy.common import classify_trend
from scalp_bot.strategy.regime import (
    HTFBias,
    LocalRegime,
    classify_htf_bias,
    classify_local_regime,
)


def candles_from_closes(
    closes: list[float],
    *,
    interval_ms: int = 60_000,
    range_pad: float = 0.03,
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


def structured_candles(
    direction: Trend,
    *,
    interval_ms: int,
    cycles: int = 8,
) -> list[Candle]:
    rows: list[Candle] = []
    pattern = [0.0, 0.7, 1.4, 0.9, 0.35, 1.0]
    index = 0
    for cycle in range(cycles):
        drift = cycle * 0.8
        for value in pattern:
            center = (
                100.0 + drift + value
                if direction == Trend.UP
                else 100.0 - drift - value
            )
            rows.append(
                Candle(
                    start_ms=index * interval_ms,
                    open=center,
                    high=center + 0.08,
                    low=center - 0.08,
                    close=center,
                    volume=100.0,
                    turnover=center * 100.0,
                    confirmed=True,
                )
            )
            index += 1
    return rows


def flat_candles(count: int, *, interval_ms: int) -> list[Candle]:
    closes = [
        100.0 + (0.02 if index % 2 else -0.02)
        for index in range(count)
    ]
    return candles_from_closes(
        closes,
        interval_ms=interval_ms,
        range_pad=0.02,
    )


def test_htf_bias_keeps_1h_direction_when_15m_is_flat() -> None:
    context_15m = flat_candles(48, interval_ms=15 * 60_000)
    context_1h = structured_candles(
        Trend.UP,
        interval_ms=60 * 60_000,
    )

    assert classify_trend(context_15m) == Trend.FLAT
    assert classify_trend(context_1h) == Trend.UP

    snapshot = classify_htf_bias(context_15m, context_1h)

    assert snapshot.bias == HTFBias.BULLISH
    assert snapshot.alignment == "1h_only"
    # Legacy strategy context still remains FLAT during Stage 1.
    assert snapshot.legacy_trend == Trend.FLAT


def test_htf_conflict_is_neutral_instead_of_directional() -> None:
    context_15m = structured_candles(
        Trend.UP,
        interval_ms=15 * 60_000,
    )
    context_1h = structured_candles(
        Trend.DOWN,
        interval_ms=60 * 60_000,
    )

    snapshot = classify_htf_bias(context_15m, context_1h)

    assert snapshot.bias == HTFBias.NEUTRAL
    assert snapshot.alignment == "conflict"


def test_local_bullish_impulse_is_detected_without_htf_permission() -> None:
    baseline = [
        100.0 + ((index % 4) - 1.5) * 0.01
        for index in range(40)
    ]
    recent = [
        100.05,
        100.16,
        100.28,
        100.41,
        100.55,
        100.70,
    ]
    candles_1m = candles_from_closes(
        [*baseline, *recent],
        range_pad=0.025,
    )
    candles_5m = flat_candles(40, interval_ms=5 * 60_000)

    snapshot = classify_local_regime(candles_1m, candles_5m)

    assert snapshot.regime == LocalRegime.BULLISH_IMPULSE
    assert snapshot.direction == Trend.UP
    assert snapshot.recent_move_pct > snapshot.impulse_threshold_pct


def test_local_bearish_impulse_overrides_bullish_parent_structure() -> None:
    baseline = [
        105.0 + ((index % 4) - 1.5) * 0.01
        for index in range(40)
    ]
    recent = [
        104.95,
        104.82,
        104.66,
        104.48,
        104.30,
        104.08,
    ]
    candles_1m = candles_from_closes(
        [*baseline, *recent],
        range_pad=0.025,
    )
    candles_5m = structured_candles(
        Trend.UP,
        interval_ms=5 * 60_000,
    )

    snapshot = classify_local_regime(candles_1m, candles_5m)

    assert snapshot.regime == LocalRegime.BEARISH_IMPULSE
    assert snapshot.direction == Trend.DOWN
    assert snapshot.parent_direction == Trend.UP


def test_moderate_counter_move_is_pullback_not_impulse() -> None:
    baseline = [
        103.0 + ((index % 4) - 1.5) * 0.012
        for index in range(40)
    ]
    recent = [
        103.00,
        102.985,
        102.970,
        102.955,
        102.940,
        102.925,
    ]
    candles_1m = candles_from_closes(
        [*baseline, *recent],
        range_pad=0.03,
    )
    candles_5m = candles_from_closes(
        structured_closes(Trend.UP),
        interval_ms=5 * 60_000,
    )

    snapshot = classify_local_regime(candles_1m, candles_5m)

    assert snapshot.regime == LocalRegime.PULLBACK
    assert snapshot.direction == Trend.DOWN
    assert snapshot.parent_direction == Trend.UP
    assert abs(snapshot.recent_move_pct) < snapshot.impulse_threshold_pct
