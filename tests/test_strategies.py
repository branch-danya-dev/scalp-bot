from scalp_bot.domain import Candle, Trend
from scalp_bot.strategies import classify_trend


def make_candle(i: int, base: float) -> Candle:
    wiggle = [0.0, 0.3, 0.1, 0.5, 0.2][i % 5]
    price = base + i * 0.2 + wiggle
    return Candle(i * 60_000, price - 0.05, price + 0.15, price - 0.15, price, 1, price)


def test_classifies_clear_rising_structure() -> None:
    candles = [make_candle(i, 100) for i in range(80)]
    assert classify_trend(candles) in {Trend.UP, Trend.FLAT}
