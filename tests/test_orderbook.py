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



def test_orderbook_gap_clears_local_state_and_requires_snapshot() -> None:
    state = OrderBookState(depth=200)
    state.apply({
        "type": "snapshot",
        "data": {
            "u": 10,
            "seq": 100,
            "b": [["100", "1"]],
            "a": [["101", "1"]],
        },
    })
    assert state.synced is True

    with pytest.raises(OrderBookSequenceError):
        state.apply({
            "type": "delta",
            "data": {
                "u": 12,
                "seq": 102,
                "b": [["100", "2"]],
                "a": [],
            },
        })

    assert state.synced is False
    assert state.bids == {}
    assert state.asks == {}

    with pytest.raises(OrderBookSequenceError):
        state.apply({
            "type": "delta",
            "data": {
                "u": 13,
                "seq": 103,
                "b": [["100", "3"]],
                "a": [],
            },
        })


def test_orderbook_fresh_snapshot_recovers_after_gap() -> None:
    state = OrderBookState(depth=200)
    state.apply({
        "type": "snapshot",
        "data": {
            "u": 20,
            "seq": 200,
            "b": [["100", "1"]],
            "a": [["101", "1"]],
        },
    })

    with pytest.raises(OrderBookSequenceError):
        state.apply({
            "type": "delta",
            "data": {
                "u": 22,
                "seq": 202,
                "b": [],
                "a": [],
            },
        })

    book = state.apply({
        "type": "snapshot",
        "data": {
            "u": 30,
            "seq": 300,
            "b": [["99", "4"]],
            "a": [["102", "5"]],
        },
    })

    assert state.synced is True
    assert state.last_update_id == 30
    assert state.last_seq == 300
    assert book.bids == [(99.0, 4.0)]
    assert book.asks == [(102.0, 5.0)]


def test_orderbook_stale_or_duplicate_delta_is_ignored() -> None:
    state = OrderBookState(depth=200)
    original = state.apply({
        "type": "snapshot",
        "data": {
            "u": 50,
            "seq": 500,
            "b": [["100", "1"]],
            "a": [["101", "1"]],
        },
    })

    duplicate = state.apply({
        "type": "delta",
        "data": {
            "u": 50,
            "seq": 500,
            "b": [["100", "9"]],
            "a": [],
        },
    })
    older_seq = state.apply({
        "type": "delta",
        "data": {
            "u": 51,
            "seq": 499,
            "b": [["100", "8"]],
            "a": [],
        },
    })

    assert duplicate.bids == original.bids
    assert older_seq.bids == original.bids
    assert state.last_update_id == 50
    assert state.last_seq == 500


def test_orderbook_u_one_reinitializes_state() -> None:
    state = OrderBookState(depth=200)
    state.apply({
        "type": "snapshot",
        "data": {
            "u": 80,
            "seq": 800,
            "b": [["100", "1"]],
            "a": [["101", "1"]],
        },
    })

    book = state.apply({
        "type": "delta",
        "data": {
            "u": 1,
            "seq": 900,
            "b": [["90", "2"]],
            "a": [["110", "3"]],
        },
    })

    assert state.synced is True
    assert state.last_update_id == 1
    assert state.last_seq == 900
    assert book.bids == [(90.0, 2.0)]
    assert book.asks == [(110.0, 3.0)]
