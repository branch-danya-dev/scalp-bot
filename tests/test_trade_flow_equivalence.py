"""Exact golden outputs captured before the trade-flow hot-path optimization."""
import json
from pathlib import Path
import random

import pytest

from scalp_bot.domain import TradeTick
from scalp_bot.strategy.common import compute_trade_flow


def flow_cases():
    rng = random.Random(20260925)
    cases = []
    for size in (0, 1, 2, 20, 100, 1500):
        for ordered in (True, False):
            trades = [TradeTick(100000 + rng.randint(-70000, 10000),
                10 ** rng.uniform(-5, 5), 10 ** rng.uniform(-5, 5),
                rng.choice(('Buy', 'SELL', 'buy', 'Sell', 'unknown'))) for _ in range(size)]
            if ordered:
                trades.sort(key=lambda t: t.ts_ms)
            cases.append((trades, 100000))
            cases.append((trades, None))
    boundary = [TradeTick(100000 + offset, 100 + i, i / 7, 'Buy' if i % 2 else 'SELL')
                for i, offset in enumerate((-60001, -60000, -20001, -20000, -15001,
                    -15000, -5001, -5000, -4999, -1, 0, 1, 99999))]
    cases.extend(((boundary, 100000), (list(reversed(boundary)), 100000)))
    return cases


@pytest.mark.parametrize('index', range(26))
def test_flow_exactly_matches_preoptimization_results(index):
    expected = json.loads(Path(__file__).with_name('trade_flow_golden.json').read_text())
    trades, now = flow_cases()[index]
    assert compute_trade_flow(trades, now) == expected[index]
