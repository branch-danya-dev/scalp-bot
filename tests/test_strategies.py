from scalp_bot.domain import Action, Candle, OrderBook, TradeTick, Trend
from scalp_bot.strategies import DensityBounceStrategy, LevelBreakoutStrategy, WeakLevelRejectionStrategy, classify_trend, detect_level_zones


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



def weak_resistance_rejection_candles() -> list[Candle]:
    candles: list[Candle] = []
    for i in range(45):
        base = 98.4 + (i % 7) * 0.05
        high = base + 0.10
        low = base - 0.10
        close = base + 0.02
        if i == 28:
            base = 99.70
            high = 100.00
            low = 99.55
            close = 99.72
        candles.append(Candle(i * 60_000, base, high, low, close, 100, 10_000))

    approach = [
        (99.25, 99.40, 99.18, 99.36),
        (99.38, 99.58, 99.32, 99.54),
        (99.55, 99.76, 99.48, 99.72),
        (99.73, 99.93, 99.66, 99.90),
        (99.91, 100.08, 99.70, 99.78),
    ]
    start = len(candles)
    for j, (o, h, l, c) in enumerate(approach):
        candles.append(Candle((start + j) * 60_000, o, h, l, c, 180, 18_000))
    return candles


def rejection_sell_flow() -> list[TradeTick]:
    start = 20_000_000
    rows: list[TradeTick] = []
    for i in range(16):
        rows.append(TradeTick(start + i * 250, 99.78, 3, "Sell"))
    for i in range(4):
        rows.append(TradeTick(start + 4_000 + i * 200, 99.78, 1, "Buy"))
    return rows


def test_weak_level_rejection_allows_runner_when_bounce_is_with_trend() -> None:
    strategy = WeakLevelRejectionStrategy()
    book = OrderBook(bids=[(99.77, 50)], asks=[(99.78, 50)])
    decision = strategy.evaluate(
        weak_resistance_rejection_candles(),
        book,
        Trend.DOWN,
        symbol="TESTUSDT",
        trades=rejection_sell_flow(),
    )
    assert decision.action == Action.SHORT
    assert decision.details["tradeMode"] == "trend_following"
    assert decision.details["allowRunner"] is True
    assert 1 <= decision.details["zone"]["touches"] <= 3


def test_weak_level_rejection_countertrend_takes_reaction_without_runner() -> None:
    strategy = WeakLevelRejectionStrategy()
    book = OrderBook(bids=[(99.77, 50)], asks=[(99.78, 50)])
    decision = strategy.evaluate(
        weak_resistance_rejection_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=rejection_sell_flow(),
    )
    assert decision.action == Action.SHORT
    assert decision.details["tradeMode"] == "countertrend_reaction"
    assert decision.details["allowRunner"] is False
    assert decision.details["exitMode"] == "reaction_only"



def density_candles_for_ask_reaction() -> list[Candle]:
    candles: list[Candle] = []
    for i in range(50):
        p = 99.0 + (i % 5) * 0.02
        candles.append(Candle(i * 60_000, p, p + 0.04, p - 0.04, p + 0.01, 100, 10_000))
    candles[-1] = Candle(49 * 60_000, 99.92, 100.00, 99.84, 99.90, 160, 16_000)
    return candles


def density_book(ask_wall_notional: float, mid_bid: float = 99.89, mid_ask: float = 99.90) -> OrderBook:
    bids = [(mid_bid - i * 0.01, 20) for i in range(25)]
    asks = [(mid_ask + i * 0.01, 20) for i in range(25)]
    wall_price = 100.00
    asks = [(p, (ask_wall_notional / p) if abs(p - wall_price) < 1e-9 else q) for p, q in asks]
    return OrderBook(bids=bids, asks=asks)


def density_reversal_sell_flow() -> list[TradeTick]:
    start = 30_000_000
    rows: list[TradeTick] = []
    for i in range(12):
        rows.append(TradeTick(start + i * 250, 99.98, 3, "Sell"))
    for i in range(3):
        rows.append(TradeTick(start + 3_000 + i * 200, 99.98, 1, "Buy"))
    return rows


def test_density_requires_persistence_then_trades_defended_fresh_wall() -> None:
    strategy = DensityBounceStrategy()
    candles = density_candles_for_ask_reaction()
    book = density_book(ask_wall_notional=20_000)

    first = strategy.evaluate(
        candles, book, Trend.DOWN, symbol="TESTUSDT", trades=[]
    )
    assert first.action == Action.WAIT
    assert first.details["state"] == "persisting"

    strategy._states["TESTUSDT"].first_seen -= 3
    second = strategy.evaluate(
        candles,
        book,
        Trend.DOWN,
        symbol="TESTUSDT",
        trades=density_reversal_sell_flow(),
    )

    assert second.action == Action.SHORT
    assert second.details["state"] == "reaction"
    assert second.details["densityFresh"] is True
    assert second.details["allowRunner"] is True


def test_density_skips_wall_that_is_being_eaten() -> None:
    strategy = DensityBounceStrategy()
    candles = density_candles_for_ask_reaction()
    book = density_book(ask_wall_notional=20_000)
    strategy.evaluate(candles, book, Trend.DOWN, symbol="TESTUSDT", trades=[])
    state = strategy._states["TESTUSDT"]
    state.first_seen -= 3
    state.peak_notional = 40_000

    decision = strategy.evaluate(
        candles,
        book,
        Trend.DOWN,
        symbol="TESTUSDT",
        trades=density_reversal_sell_flow(),
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "exhausted"


def test_density_countertrend_reaction_disables_runner() -> None:
    strategy = DensityBounceStrategy()
    candles = density_candles_for_ask_reaction()
    book = density_book(ask_wall_notional=20_000)
    strategy.evaluate(candles, book, Trend.UP, symbol="TESTUSDT", trades=[])
    strategy._states["TESTUSDT"].first_seen -= 3

    decision = strategy.evaluate(
        candles,
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=density_reversal_sell_flow(),
    )

    assert decision.action == Action.SHORT
    assert decision.details["tradeMode"] == "countertrend_reaction"
    assert decision.details["allowRunner"] is False


def test_density_removed_before_reaction_is_not_traded() -> None:
    strategy = DensityBounceStrategy()
    candles = density_candles_for_ask_reaction()
    book = density_book(ask_wall_notional=20_000)
    strategy.evaluate(candles, book, Trend.DOWN, symbol="TESTUSDT", trades=[])
    strategy._states["TESTUSDT"].first_seen -= 3

    no_wall_book = density_book(ask_wall_notional=2_000)
    no_wall_book.asks = [(p, q) for p, q in no_wall_book.asks if abs(p - 100.00) > 1e-9]
    decision = strategy.evaluate(
        candles,
        no_wall_book,
        Trend.DOWN,
        symbol="TESTUSDT",
        trades=density_reversal_sell_flow(),
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "exhausted"
    assert decision.details["reason"] == "wall_removed"
