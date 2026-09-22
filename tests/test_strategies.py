from scalp_bot.domain import Action, Candle, OrderBook, TradeTick, Trend
from scalp_bot.strategy import (
    DensityBounceStrategy,
    LevelBreakoutStrategy,
    WeakLevelRejectionStrategy,
    classify_trend,
    detect_level_zones,
)
from scalp_bot.strategy.common import approach_is_directional, nearby_round_level


def candle(i: int, o: float, h: float, l: float, c: float, volume: float = 100) -> Candle:
    return Candle(i * 60_000, o, h, l, c, volume, volume * c)


def test_classifies_clear_rising_structure() -> None:
    candles = [
        candle(i, 100 + i * 0.1, 100.2 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1)
        for i in range(80)
    ]
    assert classify_trend(candles) in {Trend.UP, Trend.FLAT}


def test_horizontal_cascade_becomes_zone_not_single_price() -> None:
    candles: list[Candle] = []
    peaks = {12: 100.00, 24: 100.05, 36: 99.98, 48: 100.03, 60: 100.01}
    for i in range(72):
        base = 98.7 + (i % 8) * 0.04
        high = peaks.get(i, base + 0.18)
        candles.append(candle(i, base, high, base - 0.15, min(base + 0.04, high - 0.02)))
    zones = detect_level_zones(candles, "resistance")
    target = next(zone for zone in zones if zone.low < 100.0 < zone.high)
    assert target.touches >= 5
    assert target.high > target.low


def test_support_directional_approach_does_not_raise_and_is_detected() -> None:
    candles = [
        candle(0, 101.0, 101.1, 100.9, 101.0),
        candle(1, 100.8, 100.9, 100.7, 100.8),
        candle(2, 100.6, 100.7, 100.5, 100.6),
        candle(3, 100.4, 100.5, 100.3, 100.4),
        candle(4, 100.2, 100.3, 100.1, 100.2),
    ]
    assert approach_is_directional(candles, "support")


def test_round_number_confluence_is_not_universal() -> None:
    assert nearby_round_level(100.01, 0.02) == 100.0
    assert nearby_round_level(123.456, 0.02) is None


def weak_support_rejection_candles() -> list[Candle]:
    rows: list[Candle] = []
    for i in range(45):
        p = 101.5 + (i % 6) * 0.04
        if i == 28:
            rows.append(candle(i, 100.35, 100.45, 100.00, 100.30))
        else:
            rows.append(candle(i, p, p + 0.10, p - 0.10, p + 0.02))
    approach = [
        (100.75, 100.82, 100.60, 100.66),
        (100.62, 100.68, 100.45, 100.50),
        (100.48, 100.54, 100.28, 100.34),
        (100.31, 100.37, 100.10, 100.16),
        (100.12, 100.22, 99.96, 100.10),
    ]
    start = len(rows)
    for j, values in enumerate(approach):
        rows.append(candle(start + j, *values, volume=180))
    return rows


def buy_flow(price: float = 100.10) -> list[TradeTick]:
    start = 20_000_000
    return [TradeTick(start + i * 200, price, 3, "Buy") for i in range(20)]


def test_weak_level_rejection_support_can_produce_long() -> None:
    strategy = WeakLevelRejectionStrategy()
    book = OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)])
    decision = strategy.evaluate(
        weak_support_rejection_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=buy_flow(),
    )
    assert decision.action == Action.LONG
    assert decision.details["tradeMode"] == "trend_following"
    assert decision.details["allowRunner"] is True


def mature_breakout_candles() -> list[Candle]:
    rows: list[Candle] = []
    peaks = {12: 100.00, 24: 100.04, 36: 99.99, 48: 100.03, 60: 100.01, 68: 100.02}
    for i in range(80):
        if i < 74:
            base = 98.9 + (i % 8) * 0.035
            high = peaks.get(i, base + 0.18)
            close = min(base + 0.06, high - 0.03)
            rows.append(candle(i, base, high, base - 0.16, close, 120))
        else:
            closes = [99.70, 99.80, 99.88, 99.95, 100.08, 100.18]
            close = closes[i - 74]
            rows.append(candle(i, close - 0.04, close + 0.06, close - 0.14, close, 300))
    return rows


def aggressive_buy_flow() -> list[TradeTick]:
    start = 30_000_000
    return [TradeTick(start + i * 200, 100.16, 8, "Buy") for i in range(24)]


def test_breakout_requires_mature_zone_and_uses_generation_once() -> None:
    strategy = LevelBreakoutStrategy()
    book = OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)])
    first = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_buy_flow(),
    )
    assert first.action == Action.LONG
    assert first.details["zone"]["touches"] >= 5
    second = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_buy_flow(),
    )
    assert second.action == Action.WAIT
    assert second.details["alreadyUsed"] is True


def density_candles() -> list[Candle]:
    rows = [candle(i, 99.5, 99.65, 99.35, 99.5) for i in range(50)]
    rows[-1] = candle(49, 99.94, 100.01, 99.88, 99.96, 180)
    return rows


def density_book(wall_notional: float, bid: float = 99.94, ask: float = 99.95) -> OrderBook:
    bids = [(bid - i * 0.01, 20) for i in range(25)]
    asks = [(ask + i * 0.01, 20) for i in range(25)]
    asks = [(p, wall_notional / p if abs(p - 100.00) < 1e-9 else q) for p, q in asks]
    return OrderBook(bids=bids, asks=asks)


def density_sell_flow() -> list[TradeTick]:
    start = 40_000_000
    rows = [TradeTick(start + i * 200, 100.00, 2, "Buy") for i in range(8)]
    rows += [TradeTick(start + 1_800 + i * 150, 99.96, 4, "Sell") for i in range(20)]
    return rows


def test_density_tracks_stability_and_trades_defended_wall() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(20_000)
    rows = density_candles()
    first = strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert first.action == Action.WAIT
    state = strategy._states["TESTUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 20_000),
        (state.first_seen + 1, 19_500),
        (state.first_seen + 2, 20_000),
    ]
    decision = strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert decision.action == Action.SHORT
    assert decision.details["setupQuality"] > 0
    assert decision.details["positionInvalidated"] is False


def test_density_removed_after_confirmed_defense_is_not_automatic_invalidation() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(20_000)
    rows = density_candles()
    strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    state = strategy._states["TESTUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 20_000),
        (state.first_seen + 1, 20_000),
        (state.first_seen + 2, 20_000),
    ]
    traded = strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert traded.action == Action.SHORT

    no_wall = density_book(2_000)
    no_wall.asks = [(p, q) for p, q in no_wall.asks if abs(p - 100.00) > 1e-9]
    after = strategy.evaluate(rows, no_wall, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert after.action == Action.WAIT
    assert after.details["positionInvalidated"] is False
