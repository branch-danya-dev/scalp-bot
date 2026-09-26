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
    assert_portable_golden(compute_trade_flow(trades, now), expected[index])


def assert_portable_golden(actual, expected):
    """Cross-platform libm/pow rounding, not a tolerance for trading PnL.

    Input prices/sizes above use 10**random_exponent. Windows/Linux libm can
    differ in the final bits before flow aggregation. Keep keys, integer counts,
    flags, zeros and nonnumeric fields exact; bound float differences by 8 ULP.
    The production function, frozen outputs and replay validator stay unchanged.
    """
    import math
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        observed = actual[key]
        assert type(observed) is type(value), key
        if isinstance(value, float) and value != 0 and math.isfinite(value):
            assert math.isfinite(observed), key
            assert abs(observed-value) <= 8*math.ulp(value), (key, observed, value)
        else:
            assert observed == value, (key, observed, value)


def test_portable_golden_keeps_counts_flags_and_material_numeric_changes_strict():
    import math
    expected = dict(count=3, ready=True, amount=100.0, zero=0.0)
    for key, wrong in (("count", 4), ("ready", False), ("amount", 100.000001),
                       ("amount", 100.0+9*math.ulp(100.0)), ("zero", math.ulp(0.0))):
        actual = dict(expected, **{key: wrong})
        with pytest.raises(AssertionError):
            assert_portable_golden(actual, expected)
