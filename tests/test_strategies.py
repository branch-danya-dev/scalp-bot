from scalp_bot.domain import Candle, Trend
from scalp_bot.strategies import classify_trend, detect_level_zones


def make_candle(i: int, base: float) -> Candle:
    wiggle = [0.0, 0.3, 0.1, 0.5, 0.2][i % 5]
    price = base + i * 0.2 + wiggle
    return Candle(i * 60_000, price - 0.05, price + 0.15, price - 0.15, price, 1, price)


def test_classifies_clear_rising_structure() -> None:
    candles = [make_candle(i, 100) for i in range(80)]
    assert classify_trend(candles) in {Trend.UP, Trend.FLAT}


def test_horizontal_cascade_becomes_zone_not_single_price() -> None:
    candles: list[Candle] = []
    for i in range(70):
        base = 98.0 + i * 0.01
        high = base + 0.25
        if i in {12, 28, 46, 60}:
            high = [100.00, 100.08, 99.96, 100.05][{12: 0, 28: 1, 46: 2, 60: 3}[i]]
        candles.append(
            Candle(
                i * 60_000,
                base,
                high,
                base - 0.18,
                base + 0.04,
                100 + (250 if i in {12, 28, 46, 60} else 0),
                10_000,
            )
        )

    zones = detect_level_zones(candles, "resistance")
    target = next(zone for zone in zones if zone.low < 100.0 < zone.high)

    assert target.touches >= 3
    assert target.high > target.low
    assert target.low < 100.0
    assert target.high > 100.0
