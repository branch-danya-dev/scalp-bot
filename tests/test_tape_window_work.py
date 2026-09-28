"""Bound repeated hot-path work without timing-dependent assertions."""
from collections import deque
import math
import random

import pytest

from scalp_bot.domain import Candle, TradeTick
from scalp_bot.engine import ActiveSymbolSession
from scalp_bot.strategy.pre_state import build_forming_candle_context


def forming():
    return Candle(60_000, 100., 102., 98., 101., 20., 2000., False)


class CountedTick:
    def __init__(self, ts):
        self.ts = ts
        self.reads = 0
        self.price = 100.

    @property
    def ts_ms(self):
        self.reads += 1
        return self.ts


def test_forming_timestamp_work_is_bounded_on_a_dense_tape():
    tape = [CountedTick(99_000 + i) for i in range(1000)]
    result = build_forming_candle_context(
        forming(), [], observed_at_ms=100_000, recent_trades=tape,
    )
    assert result.current_minute_trade_count == len(tape)
    # Selection plus one sort key and the narrower window; no repeated sorting
    # or re-filtering of the full minute. Deterministic work, not wall time.
    assert sum(t.reads for t in tape) <= 3 * len(tape)


class CountedDeque(deque):
    visited = 0

    def __iter__(self):
        for row in super().__iter__():
            self.visited += 1
            yield row


def test_book_flow_reads_each_retained_event_once():
    session = ActiveSymbolSession("TESTUSDT")
    session.book_flow = CountedDeque((99_000 + i, float(i)) for i in range(1000))
    result = session.book_flow_snapshot(100_000)
    assert result["eventCount60s"] == 1000
    assert session.book_flow.visited <= len(session.book_flow)


def reference_micro(rows, now, seconds):
    rows = sorted((t for t in rows if now-seconds*1000 <= t.ts_ms <= now),
                  key=lambda t: t.ts_ms)
    if len(rows) < 2 or rows[0].price <= 0:
        return None, None
    first = rows[0].price
    return ((rows[-1].price-first)/first*10000,
            (max(t.price for t in rows)-min(t.price for t in rows))/first*10000)


def assert_same(actual, wanted):
    if isinstance(wanted, float) and math.isnan(wanted):
        assert math.isnan(actual)
    else:
        assert actual == wanted
        if isinstance(wanted, float) and wanted == 0:
            assert math.copysign(1, actual) == math.copysign(1, wanted)


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("now", [60_000, 100_000, 120_000, 125_000])
def test_forming_matches_stable_chronological_reference(seed, now):
    rng = random.Random(seed)
    times = [59_999, 60_000, 84_999, 85_000, 94_999, 95_000,
             99_000, 99_000, 100_000, 100_001, 119_999, 120_000]
    tape = [TradeTick(ts, rng.choice([99., 100., 101., -0., 0., -1.]), 1., "Buy")
            for ts in times * 3]
    rng.shuffle(tape)
    if seed % 3 == 0:
        tape[seed].price = float("nan")
    if seed % 3 == 1:
        tape[seed].price = float("inf")
    original = list(tape)
    result = build_forming_candle_context(
        forming(), [], observed_at_ms=now, recent_trades=tape,
        last_trade_ts_ms=now-1 if seed % 2 else None,
    )
    minute = [t for t in tape if 60_000 <= t.ts_ms < 120_000 and t.ts_ms <= now]
    assert result.current_minute_trade_count == len(minute)
    latest = max([t.ts_ms for t in minute] + ([now-1] if seed % 2 else []), default=None)
    assert result.last_trade_ts_ms == latest
    for seconds in (5, 15):
        move, span = reference_micro(minute, now, seconds)
        assert_same(getattr(result, f"micro_move_{seconds}s_bps"), move)
        assert_same(getattr(result, f"micro_range_{seconds}s_bps"), span)
    assert tape == original


@pytest.mark.parametrize("seed", range(8))
def test_book_flow_preserves_input_order_sum_cutoffs_and_clock(seed):
    class Clock:
        calls = 0

        def time(self):
            self.calls += 1
            return 100.

    session = ActiveSymbolSession("TESTUSDT", activated_at=1.)
    session.clock = Clock()
    rows = [(ts, v) for ts in (39_999, 40_000, 84_999, 85_000, 94_999,
                             95_000, 100_000, 100_001)
            for v in (1.e16, 1., -1.e16, .00001)]
    random.Random(seed).shuffle(rows)
    session.book_flow = deque(rows)
    session.last_book_flow_ms = 100_001
    session.orderbook.bids = [(100., 1.)]
    session.orderbook.asks = [(101., 1.)]
    result = session.book_flow_snapshot()
    assert session.clock.calls == 1
    assert session.book_flow_snapshot(100_000) == result
    assert session.clock.calls == 1
    for seconds in (5, 15, 60):
        values = [v for ts, v in rows if 100_000-seconds*1000 <= ts <= 100_000]
        assert result[f"bestLevelOfiUsd{seconds}s"] == sum(values)
        assert result[f"eventCount{seconds}s"] == len(values)
        assert result[f"normalizedOfi{seconds}s"] == sum(values)/201.
    assert result["latestEventAgeMs"] == 0
    assert list(session.book_flow) == rows
