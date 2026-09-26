"""Preoptimization golden values; tolerate only libm-level float rounding."""
import json
import math
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
    assert_golden(compute_trade_flow(trades, now), expected[index])


def assert_golden(actual, expected):
    assert actual.keys() == expected.keys()
    for key, wanted in expected.items():
        value = actual[key]
        # flow_cases contains 10 ** rng.uniform(...). Its libm-generated input
        # is not bit-identical on every platform. Only nonzero finite floats
        # receive a four-ULP allowance, NOT a percentage/monetary tolerance.
        # Discrete decisions, counts, zeros and missing values remain exact.
        if type(wanted) is float and wanted != 0 and math.isfinite(wanted):
            assert type(value) is float and math.isfinite(value), key
            assert abs(value-wanted) <= 4*math.ulp(wanted), (key, value, wanted)
        else:
            assert type(value) is type(wanted) and value == wanted, (key, value, wanted)


@pytest.mark.parametrize("expected,actual", [
    ({"participationConfirmed": True}, {"participationConfirmed": False}),
    ({"tradeCount5s": 3}, {"tradeCount5s": 4}),
    ({"cvd5s": 0.0}, {"cvd5s": 1e-15}),
    ({"cvd5s": 100.0}, {"cvd5s": 100.0+8*math.ulp(100.0)}),
])
def test_golden_portability_does_not_hide_decisions_or_material_changes(expected, actual):
    with pytest.raises(AssertionError):
        assert_golden(actual, expected)
