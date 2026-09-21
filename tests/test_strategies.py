from scalp_bot.domain import Action, Candle, OrderBook, TradeTick, Trend
from scalp_bot.strategies import LevelBreakoutStrategy, classify_trend, detect_level_zones


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


def breakout_candles() -> list[Candle]:
    candles: list[Candle] = []
    peaks = {18: 100.00, 34: 100.04, 50: 99.98, 64: 100.03}
    for i in range(80):
        if i < 74:
            base = 98.8 + (i % 8) * 0.035
            high = peaks.get(i, base + 0.18)
            low = base - 0.16
            close = min(base + 0.06, high - 0.03)
        else:
            closes = [99.70, 99.78, 99.86, 99.93, 100.04, 100.15]
            lows = [99.48, 99.58, 99.68, 99.76, 99.84, 99.92]
            close = closes[i - 74]
            low = lows[i - 74]
            high = close + 0.06
            base = close - 0.04
        candles.append(
            Candle(
                i * 60_000,
                base,
                high,
                low,
                close,
                300 if i >= 77 else 100,
                10_000,
            )
        )
    return candles


def aggressive_trades(side: str) -> list[TradeTick]:
    start = 10_000_000
    return [
        TradeTick(start + i * 250, 100.14, 8 if side == "Buy" else 2, side)
        for i in range(20)
    ]


def test_breakout_waits_when_trade_flow_does_not_confirm() -> None:
    strategy = LevelBreakoutStrategy()
    book = OrderBook(bids=[(100.14, 50)], asks=[(100.15, 50)])
    decision = strategy.evaluate(
        breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_trades("Sell"),
    )
    assert decision.action == Action.WAIT
    assert decision.details["state"] in {"break", "approach", "pressure"}


def test_breakout_enters_when_zone_break_and_flow_align() -> None:
    strategy = LevelBreakoutStrategy()
    book = OrderBook(bids=[(100.14, 50)], asks=[(100.15, 50)])
    decision = strategy.evaluate(
        breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_trades("Buy"),
    )
    assert decision.action == Action.LONG
    assert decision.details["state"] == "impulse"
    assert decision.stop < decision.entry < decision.target
    assert decision.details["zone"]["touches"] >= 2


def test_breakout_state_is_isolated_per_symbol() -> None:
    strategy = LevelBreakoutStrategy()
    book = OrderBook(bids=[(100.14, 50)], asks=[(100.15, 50)])
    first = strategy.evaluate(
        breakout_candles(), book, Trend.UP, symbol="AAAUSDT", trades=aggressive_trades("Buy")
    )
    second = strategy.evaluate(
        breakout_candles(), book, Trend.UP, symbol="BBBUSDT", trades=aggressive_trades("Buy")
    )
    assert first.action == Action.LONG
    assert second.action == Action.LONG
