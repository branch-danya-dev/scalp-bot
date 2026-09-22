import pytest

from scalp_bot.bybit import OrderBookSequenceError, OrderBookState


def test_orderbook_sequence_gap_is_detected() -> None:
    state = OrderBookState(depth=200)
    state.apply({
        "type": "snapshot",
        "data": {"u": 10, "b": [["100", "1"]], "a": [["101", "1"]]},
    })
    with pytest.raises(OrderBookSequenceError):
        state.apply({"type": "delta", "data": {"u": 12, "b": [], "a": []}})


def test_orderbook_depth_keeps_more_than_50_levels() -> None:
    state = OrderBookState(depth=200)
    bids = [[str(100 - i * 0.01), "1"] for i in range(120)]
    asks = [[str(101 + i * 0.01), "1"] for i in range(120)]
    book = state.apply({
        "type": "snapshot",
        "data": {"u": 1, "b": bids, "a": asks},
    })
    assert len(book.bids) == 120
    assert len(book.asks) == 120
