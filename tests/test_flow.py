from collections import deque

from scalp_bot.domain import TradeTick
from scalp_bot.strategy.common import compute_trade_flow
from scalp_bot.strategy.flow import (
    cumulative_delta,
    flow_at_level,
    flow_beyond_level,
    prune_trades,
)


def test_trade_buffer_prunes_by_time_not_count() -> None:
    rows = deque([
        TradeTick(1_000, 100, 1, "Buy"),
        TradeTick(95_000, 100, 1, "Buy"),
        TradeTick(100_000, 100, 1, "Sell"),
    ])
    prune_trades(rows, 100_000, 10)
    assert len(rows) == 2
    assert rows[0].ts_ms == 95_000


def test_flow_at_level_detects_absorption() -> None:
    rows = [
        TradeTick(10_000 + i * 100, 100.0 + (i % 2) * 0.001, 10, "Buy")
        for i in range(20)
    ]
    flow = flow_at_level(rows, 100.0, seconds=15)
    assert flow.buy_notional > 0
    assert flow.absorption_efficiency > 0.5
    assert cumulative_delta(rows, 15) > 0



def test_cvd_windows_are_independent() -> None:
    now = 100_000
    rows = [
        TradeTick(now - 50_000, 100, 1, "Buy"),
        TradeTick(now - 12_000, 100, 2, "Sell"),
        TradeTick(now - 3_000, 100, 3, "Buy"),
    ]

    assert cumulative_delta(rows, 5, now) == 300
    assert cumulative_delta(rows, 15, now) == 100
    assert cumulative_delta(rows, 60, now) == 200

    flow = compute_trade_flow(rows, now)
    assert flow["cvd5s"] == 300
    assert flow["cvd15s"] == 100
    assert flow["cvd60s"] == 200


def test_empty_trade_flow_keeps_cvd_schema() -> None:
    flow = compute_trade_flow([])
    assert flow["cvd5s"] == 0
    assert flow["cvd15s"] == 0
    assert flow["cvd60s"] == 0


def test_time_buffer_can_keep_more_than_two_thousand_recent_trades() -> None:
    now = 100_000
    rows = deque(
        TradeTick(now - 5_000 + i, 100, 0.01, "Buy")
        for i in range(3_000)
    )
    prune_trades(rows, now, 90)
    assert len(rows) == 3_000


def test_best_level_ofi_tracks_bid_additions_and_ask_withdrawals() -> None:
    from scalp_bot.domain import OrderBook
    from scalp_bot.strategy.flow import best_level_ofi_usd

    previous = OrderBook(
        bids=[(100.0, 1.0)],
        asks=[(101.0, 1.0)],
    )
    stronger_bid = OrderBook(
        bids=[(100.0, 2.0)],
        asks=[(101.0, 1.0)],
    )
    assert best_level_ofi_usd(previous, stronger_bid) == 100.0

    ask_withdrawal = OrderBook(
        bids=[(100.0, 1.0)],
        asks=[(101.0, 0.5)],
    )
    assert best_level_ofi_usd(previous, ask_withdrawal) == 50.5


def test_best_level_ofi_is_negative_when_best_bid_steps_down() -> None:
    from scalp_bot.domain import OrderBook
    from scalp_bot.strategy.flow import best_level_ofi_usd

    previous = OrderBook(
        bids=[(100.0, 1.0)],
        asks=[(101.0, 1.0)],
    )
    weaker = OrderBook(
        bids=[(99.0, 1.0)],
        asks=[(101.0, 1.0)],
    )
    assert best_level_ofi_usd(previous, weaker) == -100.0


def test_trade_flow_expires_against_current_observation_time() -> None:
    last_trade = 100_000
    rows = [
        TradeTick(last_trade - 1_000, 100, 1, "Buy"),
        TradeTick(last_trade, 100, 1, "Buy"),
    ]

    flow = compute_trade_flow(rows, last_trade + 6_000)
    assert flow["tradeCount5s"] == 0
    assert flow["cvd5s"] == 0
    assert flow["latestTradeAgeMs"] == 6_000

    local = flow_at_level(
        rows,
        100,
        seconds=5,
        now_ms=last_trade + 6_000,
    )
    assert local.trade_count == 0


def test_flow_windows_ignore_future_rows() -> None:
    now = 100_000
    rows = [
        TradeTick(now - 1_000, 100, 1, "Buy"),
        TradeTick(now + 1_000, 100, 10, "Sell"),
    ]
    flow = compute_trade_flow(rows, now)
    assert flow["buyNotional5s"] == 100
    assert flow["sellNotional5s"] == 0
    assert cumulative_delta(rows, 5, now) == 100


def test_flow_beyond_level_requires_executions_on_broken_side() -> None:
    now = 100_000
    rows = [
        TradeTick(now - 2_000, 99.99, 2, "Buy"),
        TradeTick(now - 1_000, 100.00, 2, "Buy"),
        TradeTick(now - 500, 100.02, 2, "Buy"),
    ]

    above = flow_beyond_level(
        rows,
        100.01,
        long_side=True,
        seconds=5,
        now_ms=now,
    )

    assert above.trade_count == 1
    assert above.buy_notional > 0


def test_trade_flow_requires_relative_participation_baseline() -> None:
    now = 100_000
    no_baseline = [
        TradeTick(now - 2_000 + i * 300, 100, 1, "Buy")
        for i in range(5)
    ]
    cold = compute_trade_flow(no_baseline, now)
    assert cold["baselineReady"] is False
    assert cold["participationConfirmed"] is False

    rows = [
        TradeTick(now - 18_000 + i * 2_000, 100, 1, "Sell")
        for i in range(6)
    ]
    rows += [
        TradeTick(now - 4_000 + i * 500, 100, 2, "Buy")
        for i in range(8)
    ]
    active = compute_trade_flow(rows, now)
    assert active["baselineReady"] is True
    assert active["participationConfirmed"] is True
    assert (
        active["tradeRateRatio"] >= 1
        or active["tradeSizeRatio"] >= 1
        or active["acceleration"] >= 1
    )


def test_trade_flow_rejects_busy_but_weaker_notional_pace() -> None:
    now = 100_000
    rows = [
        TradeTick(now - 18_000 + i * 2_000, 100, 5, "Sell")
        for i in range(6)
    ]
    rows += [
        TradeTick(now - 4_000 + i * 400, 100, 0.5, "Buy")
        for i in range(10)
    ]

    flow = compute_trade_flow(rows, now)

    assert flow["baselineReady"] is True
    assert flow["tradeRateRatio"] > 1.0
    assert flow["tradeSizeRatio"] < 1.0
    assert flow["acceleration"] < 1.0
    assert flow["participationConfirmed"] is False


def test_flow_beyond_level_does_not_count_boundary_print() -> None:
    now = 100_000
    rows = [
        TradeTick(now - 500, 100.01, 2, "Buy"),
    ]

    flow = flow_beyond_level(
        rows,
        100.01,
        long_side=True,
        seconds=5,
        now_ms=now,
    )

    assert flow.trade_count == 0
