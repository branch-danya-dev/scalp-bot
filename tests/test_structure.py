from datetime import datetime, timezone

import pytest

from scalp_bot.domain import Candle
from scalp_bot.strategy.structure import (
    aggregate_candles,
    MarketStructure,
    StructuralLevel,
    build_market_structure,
    detect_trendline,
)


def c(i: int, o: float, h: float, l: float, close: float, volume: float = 100) -> Candle:
    return Candle(i * 60_000, o, h, l, close, volume, volume * close)


def test_aggregate_candles_builds_5m_bars() -> None:
    rows = [c(i, 100+i, 101+i, 99+i, 100.5+i) for i in range(10)]
    bars = aggregate_candles(rows, 5)
    assert len(bars) == 2
    assert bars[0].open == rows[0].open
    assert bars[0].close == rows[4].close
    assert bars[0].high == max(x.high for x in rows[:5])
    assert bars[0].low == min(x.low for x in rows[:5])


def test_market_structure_contains_current_day_high_and_low() -> None:
    day = int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp() * 1000)
    context = [
        Candle(day + i * 900_000, 100, 101 + i * 0.1, 99 - i * 0.05, 100, 100, 10_000)
        for i in range(20)
    ]
    one_min = [c(i, 100, 100.2, 99.8, 100) for i in range(80)]
    structure = build_market_structure(one_min, context, 100)
    assert structure.day_high == max(x.high for x in context)
    assert structure.day_low == min(x.low for x in context)
    assert any(level.kind == "day_high" for level in structure.levels)
    assert any(level.kind == "day_low" for level in structure.levels)


def test_repeated_rising_lows_form_diagonal_support() -> None:
    rows = []
    pivot_lows = {10: 100.0, 25: 100.5, 40: 101.0, 55: 101.5}
    for i in range(70):
        base = 102 + i * 0.02
        low = pivot_lows.get(i, base - 0.15)
        rows.append(c(i, base, base + 0.15, low, base + 0.02))
    line = detect_trendline(rows, "support", "1m")
    assert line is not None
    assert line.touches >= 3
    assert line.slope_per_bar > 0



def test_market_structure_separates_current_and_previous_utc_day() -> None:
    previous_day = int(
        datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp() * 1000
    )
    current_day = int(
        datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp() * 1000
    )

    previous = [
        Candle(
            previous_day + i * 900_000,
            100,
            105 + i * 0.01,
            95 - i * 0.01,
            100,
            100,
            10_000,
        )
        for i in range(96)
    ]
    current = [
        Candle(
            current_day + i * 900_000,
            110,
            112 + i * 0.01,
            108 - i * 0.01,
            110,
            100,
            10_000,
        )
        for i in range(20)
    ]
    one_min = [c(i, 110, 110.1, 109.9, 110) for i in range(80)]

    structure = build_market_structure(
        one_min,
        previous + current,
        110,
    )

    assert structure.previous_day_high == max(x.high for x in previous)
    assert structure.previous_day_low == min(x.low for x in previous)
    assert structure.day_high == max(x.high for x in current)
    assert structure.day_low == min(x.low for x in current)

    kinds = {level.kind for level in structure.levels}
    assert "previous_day_high" in kinds
    assert "previous_day_low" in kinds
    assert "day_high" in kinds
    assert "day_low" in kinds



def test_nearest_directional_includes_day_and_previous_day_levels() -> None:
    ordinary = StructuralLevel(
        kind="resistance",
        low=101.0,
        high=101.1,
        touches=4,
        timeframe="5m",
        score=0.8,
    )
    previous_day = StructuralLevel(
        kind="previous_day_high",
        low=100.5,
        high=100.5,
        touches=1,
        timeframe="1D",
        score=0.88,
    )
    current_day_low = StructuralLevel(
        kind="day_low",
        low=99.4,
        high=99.4,
        touches=1,
        timeframe="1D",
        score=0.92,
    )
    structure = MarketStructure(
        levels=[
            ordinary,
            previous_day,
            current_day_low,
        ]
    )

    resistance = structure.nearest_directional(
        100.0,
        "resistance",
    )
    support = structure.nearest_directional(
        100.0,
        "support",
    )

    assert resistance is previous_day
    assert support is current_day_low



def test_current_day_extremes_include_newer_confirmed_one_minute_data() -> None:
    day = int(
        datetime(
            2026,
            9,
            22,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )
    context = [
        Candle(
            day + i * 900_000,
            100,
            101.0,
            99.0,
            100,
            100,
            10_000,
            confirmed=True,
        )
        for i in range(8)
    ]
    one_min = [
        Candle(
            day + 8 * 900_000 + i * 60_000,
            100,
            103.0 if i == 5 else 100.5,
            97.5 if i == 7 else 99.5,
            100,
            100,
            10_000,
            confirmed=True,
        )
        for i in range(20)
    ]

    structure = build_market_structure(
        one_min,
        context,
        100,
    )

    assert structure.day_high == pytest.approx(103.0)
    assert structure.day_low == pytest.approx(97.5)
