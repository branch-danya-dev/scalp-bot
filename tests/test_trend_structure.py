from scalp_bot.domain import Action, Candle, OrderBook, TradeTick, Trend
from scalp_bot.strategy.structure import MarketStructure, TrendLine
from scalp_bot.strategy.trend_structure import TrendStructureStrategy


def candle(
    i: int,
    close: float,
    *,
    low: float | None = None,
    high: float | None = None,
) -> Candle:
    return Candle(
        start_ms=i * 60_000,
        open=close,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=1000,
        turnover=100_000,
        confirmed=True,
    )


def long_pullback_candles() -> list[Candle]:
    rows = [candle(i, 99.0 + i * 0.01) for i in range(35)]
    rows.extend([
        candle(35, 101.00, high=101.05, low=100.95),
        candle(36, 100.80, high=100.85, low=100.75),
        candle(37, 100.60, high=100.65, low=100.55),
        candle(38, 100.40, high=100.45, low=100.35),
        candle(39, 100.10, high=100.20, low=99.99),
    ])
    return rows


def short_pullback_candles() -> list[Candle]:
    rows = [candle(i, 101.0 - i * 0.01) for i in range(35)]
    rows.extend([
        candle(35, 99.00, high=99.05, low=98.95),
        candle(36, 99.20, high=99.25, low=99.15),
        candle(37, 99.40, high=99.45, low=99.35),
        candle(38, 99.60, high=99.65, low=99.55),
        candle(39, 99.90, high=100.01, low=99.80),
    ])
    return rows


def structure(kind: str) -> MarketStructure:
    return MarketStructure(
        trendlines=[
            TrendLine(
                kind=kind,
                timeframe="5m",
                start_ms=10 * 60_000,
                end_ms=39 * 60_000,
                start_price=99.7 if kind == "support" else 100.3,
                end_price=100.0,
                current_price=100.0,
                touches=4,
                score=0.80,
                slope_per_bar=0.01 if kind == "support" else -0.01,
            )
        ]
    )


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook(
        bids=[(bid, 100)],
        asks=[(ask, 100)],
    )


def buy_flow() -> list[TradeTick]:
    return [
        TradeTick(100_000 + i * 100, 100.0, 10, "Buy")
        for i in range(6)
    ]


def sell_flow() -> list[TradeTick]:
    return [
        TradeTick(100_000 + i * 100, 100.0, 10, "Sell")
        for i in range(6)
    ]


def test_trend_without_confirmed_line_does_not_fallback_to_two_swings() -> None:
    strategy = TrendStructureStrategy()
    decision = strategy.evaluate(
        long_pullback_candles(),
        book(100.09, 100.11),
        Trend.UP,
        symbol="AAAUSDT",
        trades=buy_flow(),
        structure=MarketStructure(),
    )

    assert decision.action == Action.WAIT
    assert "3 опорами" in decision.reasons[0]


def test_touch_and_rejection_do_not_create_immediate_long_entry() -> None:
    strategy = TrendStructureStrategy()
    decision = strategy.evaluate(
        long_pullback_candles(),
        book(100.09, 100.11),
        Trend.UP,
        symbol="AAAUSDT",
        trades=buy_flow(),
        structure=structure("support"),
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "test"
    assert decision.details["reclaimLevel"] > 100.1


def test_reclaim_without_trade_flow_is_still_wait() -> None:
    strategy = TrendStructureStrategy()
    candles = long_pullback_candles()
    market_structure = structure("support")

    strategy.evaluate(
        candles,
        book(100.09, 100.11),
        Trend.UP,
        symbol="AAAUSDT",
        trades=[],
        structure=market_structure,
    )
    decision = strategy.evaluate(
        candles,
        book(100.69, 100.71),
        Trend.UP,
        symbol="AAAUSDT",
        trades=[],
        structure=market_structure,
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "test"
    assert decision.details["reclaimed"] is True
    assert decision.details["flowConfirmed"] is False


def test_long_entry_requires_test_reclaim_flow_and_follow_through() -> None:
    strategy = TrendStructureStrategy()
    candles = long_pullback_candles()
    market_structure = structure("support")
    trades = buy_flow()

    first = strategy.evaluate(
        candles,
        book(100.09, 100.11),
        Trend.UP,
        symbol="AAAUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert first.action == Action.WAIT
    assert first.details["state"] == "test"

    reclaim = strategy.evaluate(
        candles,
        book(100.69, 100.71),
        Trend.UP,
        symbol="AAAUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert reclaim.action == Action.WAIT
    assert reclaim.details["state"] == "reclaim"

    entry = strategy.evaluate(
        candles,
        book(100.79, 100.81),
        Trend.UP,
        symbol="AAAUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert entry.action == Action.LONG
    assert entry.details["state"] == "continuation"
    assert entry.details["flowConfirmed"] is True
    assert entry.stop < entry.entry

    repeated = strategy.evaluate(
        candles,
        book(100.89, 100.91),
        Trend.UP,
        symbol="AAAUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert repeated.action == Action.WAIT
    assert repeated.details["alreadyUsed"] is True


def test_short_entry_uses_symmetric_confirmation_sequence() -> None:
    strategy = TrendStructureStrategy()
    candles = short_pullback_candles()
    market_structure = structure("resistance")
    trades = sell_flow()

    first = strategy.evaluate(
        candles,
        book(99.89, 99.91),
        Trend.DOWN,
        symbol="SHORTUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert first.action == Action.WAIT
    assert first.details["state"] == "test"

    reclaim = strategy.evaluate(
        candles,
        book(99.34, 99.36),
        Trend.DOWN,
        symbol="SHORTUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert reclaim.action == Action.WAIT
    assert reclaim.details["state"] == "reclaim"

    entry = strategy.evaluate(
        candles,
        book(99.24, 99.26),
        Trend.DOWN,
        symbol="SHORTUSDT",
        trades=trades,
        structure=market_structure,
    )
    assert entry.action == Action.SHORT
    assert entry.stop > entry.entry
