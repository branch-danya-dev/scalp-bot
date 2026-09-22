from collections import deque

from scalp_bot.domain import TradeTick
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
