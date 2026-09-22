from collections import deque

from scalp_bot.domain import TradeTick
from scalp_bot.strategy.common import compute_trade_flow
from scalp_bot.strategy.flow import cumulative_delta, flow_at_level, prune_trades


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
