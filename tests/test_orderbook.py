import asyncio
from time import perf_counter_ns

import pytest

from scalp_bot.domain import OrderBook, Side
from scalp_bot.bybit import (
    MarketDataBackpressureError,
    MarketMessage,
    OrderBookSequenceError,
    OrderBookState,
    _kline_is_confirmed,
    _process_market_queue,
    _stream_topics,
    decode_market_message,
)


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



def test_orderbook_depth_1000_preserves_full_requested_window() -> None:
    state = OrderBookState(depth=1000)
    bids = [[str(100 - i * 0.001), "1"] for i in range(1000)]
    asks = [[str(101 + i * 0.001), "1"] for i in range(1000)]

    book = state.apply({
        "type": "snapshot",
        "data": {
            "u": 1,
            "seq": 1,
            "b": bids,
            "a": asks,
        },
    })

    assert len(book.bids) == 1000
    assert len(book.asks) == 1000


def test_rest_kline_confirmation_respects_open_interval() -> None:
    start = 1_000_000
    assert not _kline_is_confirmed(
        start,
        "15",
        now_ms=start + 14 * 60_000,
    )
    assert _kline_is_confirmed(
        start,
        "15",
        now_ms=start + 15 * 60_000,
    )


def test_orderbook_entry_vwap_walks_visible_depth() -> None:
    book = OrderBook(
        bids=[(99.0, 10)],
        asks=[(100.0, 1), (101.0, 2)],
    )

    vwap, filled = book.entry_vwap(Side.LONG, 200)

    assert filled == pytest.approx(200)
    assert vwap == pytest.approx(200 / (1 + 100 / 101))
    assert vwap > 100.0


def test_msgspec_market_decoder_returns_typed_message() -> None:
    message = decode_market_message(
        b'{"topic":"orderbook.50.BTCUSDT","type":"snapshot",'
        b'"ts":123,"cts":122,"data":{"u":1,"seq":2,'
        b'"b":[["100","1"]],"a":[["101","2"]]},'
        b'"unknown":"ignored"}'
    )

    assert isinstance(message, MarketMessage)
    assert message.topic == "orderbook.50.BTCUSDT"
    assert message.type == "snapshot"
    assert message.ts == 123
    assert message.cts == 122

    state = OrderBookState(depth=50)
    book = state.apply(message)
    assert book.best_bid == pytest.approx(100.0)
    assert book.best_ask == pytest.approx(101.0)


@pytest.mark.asyncio
async def test_ws_reader_is_decoupled_from_slow_market_callback(
    monkeypatch,
) -> None:
    stop = asyncio.Event()
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    second_received = asyncio.Event()

    payloads = [
        (
            b'{"topic":"publicTrade.BTCUSDT","ts":1,'
            b'"data":[{"T":1,"p":"100","v":"1","S":"Buy"}]}'
        ),
        (
            b'{"topic":"publicTrade.BTCUSDT","ts":2,'
            b'"data":[{"T":2,"p":"101","v":"1","S":"Buy"}]}'
        ),
    ]

    class FakeWebSocket:
        def __init__(self) -> None:
            self.recv_count = 0

        async def send(self, payload, text=None) -> None:
            return None

        async def recv(self, decode=None):
            self.recv_count += 1
            if self.recv_count == 1:
                return payloads[0]
            if self.recv_count == 2:
                second_received.set()
                return payloads[1]
            await stop.wait()
            raise ConnectionError("fixture stream finished")

    ws = FakeWebSocket()

    class FakeConnect:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "scalp_bot.bybit.websockets.connect",
        lambda *args, **kwargs: FakeConnect(),
    )

    async def slow_callback(message: MarketMessage) -> None:
        callback_started.set()
        await release_callback.wait()

    task = asyncio.create_task(
        _stream_topics(
            "wss://example.invalid",
            ["publicTrade.BTCUSDT"],
            slow_callback,
            stop,
            queue_size=4,
            queue_put_timeout_seconds=0.05,
            queue_max_lag_seconds=1.0,
        )
    )

    await asyncio.wait_for(
        callback_started.wait(),
        timeout=0.5,
    )
    await asyncio.wait_for(
        second_received.wait(),
        timeout=0.5,
    )
    assert ws.recv_count >= 2

    release_callback.set()
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_market_processor_rejects_stale_backlog() -> None:
    queue: asyncio.Queue[MarketMessage] = asyncio.Queue()
    message = MarketMessage(
        topic="orderbook.50.BTCUSDT",
        received_at_ns=(
            perf_counter_ns() - 1_000_000_000
        ),
    )
    queue.put_nowait(message)
    callback_called = False

    async def callback(_message: MarketMessage) -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(MarketDataBackpressureError):
        await _process_market_queue(
            queue,
            callback,
            asyncio.Event(),
            max_lag_seconds=0.01,
        )

    assert callback_called is False
