from scalp_bot.domain import Candle, OrderBook, TradeTick
from scalp_bot.strategy.common import compute_level_flow, compute_trade_flow
from scalp_bot.strategy.density import DensityBounceStrategy
from scalp_bot.config import Settings
from scalp_bot.bybit import OrderBookState, OrderBookSyncError


def test_trade_flow_keeps_60_second_cvd() -> None:
    trades = [
        TradeTick(1_000 + i * 1_000, 100, 2, "Buy")
        for i in range(30)
    ] + [
        TradeTick(31_000 + i * 1_000, 100, 1, "Sell")
        for i in range(20)
    ]
    flow = compute_trade_flow(trades, now_ms=50_000)
    assert flow["tradeCount60s"] == 50
    assert flow["cvdNotional60s"] > 0
    assert flow["buyNotional15s"] >= 0


def test_level_flow_is_price_local() -> None:
    trades = [
        TradeTick(10_000, 100.00, 10, "Buy"),
        TradeTick(11_000, 100.01, 5, "Sell"),
        TradeTick(12_000, 101.00, 100, "Sell"),
    ]
    flow = compute_level_flow(
        trades,
        100.0,
        tolerance_pct=0.0002,
        now_ms=12_000,
    )
    assert flow["tradeCount"] == 2
    assert flow["deltaNotional"] > 0


def test_orderbook_rejects_delta_before_snapshot() -> None:
    state = OrderBookState(1000)
    try:
        state.apply({
            "type": "delta",
            "data": {
                "u": 2,
                "seq": 2,
                "b": [["100", "1"]],
                "a": [],
            },
        })
        assert False, "expected OrderBookSyncError"
    except OrderBookSyncError:
        pass


def test_orderbook_depth_keeps_more_than_50_levels() -> None:
    state = OrderBookState(1000)
    bids = [[str(100 - i * 0.01), "1"] for i in range(200)]
    asks = [[str(100.01 + i * 0.01), "1"] for i in range(200)]
    book = state.apply({
        "type": "snapshot",
        "data": {"u": 1, "seq": 1, "b": bids, "a": asks},
    })
    assert len(book.bids) == 200
    assert len(book.asks) == 200


def test_density_requires_absolute_wall_notional() -> None:
    strategy = DensityBounceStrategy()
    strategy.configure(Settings(density_min_wall_notional_usd=100_000))
    bids = [(99.99 - i * 0.01, 10) for i in range(100)]
    asks = [(100.01 + i * 0.01, 10) for i in range(100)]
    # 20x the local ~1k median, but still only ~20k notional.
    asks[10] = (asks[10][0], 200)
    book = OrderBook(bids=bids, asks=asks)
    bid_rows, ask_rows, baseline = strategy._rows(book)
    assert strategy._select_wall(book, bid_rows, ask_rows, baseline) is None
