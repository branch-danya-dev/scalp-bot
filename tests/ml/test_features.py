"""Pure adapter checks on actual context classes, not end-to-end ML parity."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from scalp_bot.domain import Candle, OrderBook, Trend
from scalp_bot.ml.contracts import SnapshotRef
from scalp_bot.ml.features import ContextCoverage, FEATURE_FIELDS, FEATURE_NAMES, FEATURE_SCHEMA, extract_context_features, schema_description
from scalp_bot.strategy.flow_context import FlowAlignment, FlowAlignmentClass, FlowHorizon, MultiHorizonFlowContext
from scalp_bot.strategy.liquidity_evidence import LiquidityEvidence, LiquidityEvidenceState
from scalp_bot.strategy.market_context import ExecutionContext, MarketContext, StructureContext
from scalp_bot.strategy.pre_state import build_forming_candle_context

NOW = 120000


def context():
    execution = ExecutionContext(True, True, .1, True, .2, .0001, 100., 100.01, 200., 100., 300., 60.)
    alignment = FlowAlignment(FlowAlignmentClass.ALIGNED, .1, [5], [], [], [])
    flow = MultiHorizonFlowContext(NOW, {n: FlowHorizon(n, .2, 20., 100., 5, 2., .02, 2, .1, Trend.UP)
                                       for n in (5, 15, 60)}, Trend.UP, .1, .1, alignment, alignment)
    liquidity = LiquidityEvidence(LiquidityEvidenceState.TRACKING, "bid", 99., "found", None, Trend.UP,
        .5, True, .9, .1, .01, .2, False, 4., .01, False, False, [], None, [])
    structure = StructureContext(100., 2, 0, None, None, .01, .02, None, None)
    candle = Candle(start_ms=NOW, open=100., high=101., low=99., close=100.5, volume=10., turnover=1000., confirmed=False)
    forming = build_forming_candle_context(candle, [], observed_at_ms=NOW)
    assert forming is not None
    return MarketContext("NEARUSDT", NOW, 100.5, Trend.UP, None, None, flow, liquidity,
                         structure, execution, forming)


def ref():
    return SnapshotRef("cap", "NEARUSDT", 1, 10, NOW, 1000, "clock-A", FEATURE_SCHEMA)


def covered():
    return ContextCoverage((5, 15, 60), (5, 15, 60), True, True, True, True)


def extract(ctx=None, r=None, c=None):
    return extract_context_features(ctx or context(), r or ref(), c or covered())


def values(snapshot): return dict(zip(snapshot.names, snapshot.values))


def test_schema_units_order_and_detached_copy():
    original = context()
    snapshot = extract(original)
    result = values(snapshot)
    assert snapshot.names == FEATURE_NAMES and len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    assert result["spread_bps"] == pytest.approx(1.)
    assert result["body_bps"] == pytest.approx(50.)
    assert result["top5_depth_usd"] == 300.
    assert result["top5_depth_imbalance"] == pytest.approx(1/3)
    assert result["support_distance_bps"] == 100.
    assert result["wall_distance_bps"] == 100.
    original.flow.horizons[5].cvd_usd = 999.
    original.liquidity.remaining_ratio = .1
    assert values(snapshot)["cvd_usd_5s"] == 20.
    assert values(snapshot)["remaining_ratio"] == .9
    assert extract(deepcopy(context())) == extract(context())
    serialized = json.loads(json.dumps(schema_description()))
    serialized["fields"].clear()
    assert schema_description()["fields"]
    assert all("target" not in name and "pnl" not in name for name in snapshot.names)


@pytest.mark.parametrize("changes", [{"symbol": "ETHUSDT"}, {"market_time_ms": NOW+1}, {"feature_schema": "old"}])
def test_wrong_reference(changes):
    with pytest.raises(ValueError):
        extract(r=replace(ref(), **changes))


def test_unknown_is_not_observed_zero():
    unknown = values(extract(c=ContextCoverage()))
    assert unknown["trade_covered_5s"] == 0.
    assert unknown["trade_count_5s"] is None
    assert unknown["cvd_usd_5s"] is None
    assert unknown["wall_present"] is None
    assert unknown["top5_depth_usd"] is None
    ctx = context()
    ctx.flow.horizons[5] = replace(ctx.flow.horizons[5], trade_count=0, cvd_usd=0., trade_notional_usd=0.)
    known = values(extract(ctx))
    assert known["trade_covered_5s"] == 1. and known["trade_count_5s"] == 0.
    assert known["cvd_usd_5s"] == 0.
    assert known["trade_imbalance_5s"] is None


def test_trade_coverage_does_not_imply_book_coverage():
    result = values(extract(c=replace(covered(), ofi_windows=())))
    assert result["trade_covered_5s"] == 1.
    assert result["ofi_covered_5s"] == 0. and result["normalized_ofi_5s"] is None


@pytest.mark.parametrize("changes", [{"book_fresh": False}, {"book_synced": False}, {"book_synced": None}, {"best_ask": 99.}])
def test_bad_book_masks_book_dependent_features(changes):
    ctx = context()
    result = values(extract(replace(ctx, execution=replace(ctx.execution, **changes))))
    assert result["book_ready"] == 0.
    assert result["spread_bps"] is None and result["top5_depth_usd"] is None
    assert result["ofi_covered_5s"] == 0. and result["liquidity_known"] == 0.
    assert result["trade_count_5s"] == 5.  # Independent healthy trade window.


def test_units_and_liquidity_unknown_masks():
    result = values(extract(c=replace(covered(), quote_units_verified=False)))
    assert result["trade_count_5s"] == 5.
    assert result["trade_notional_usd_5s"] is None and result["cvd_usd_5s"] is None
    assert result["normalized_ofi_5s"] is None
    ctx = context()
    ctx.liquidity.state = LiquidityEvidenceState.UNKNOWN
    assert values(extract(ctx))["wall_present"] is None


def test_flow_not_refreshed_by_new_context_time():
    ctx = context()
    ctx.flow.observed_at_ms = NOW-1
    result = values(extract(ctx))
    assert result["trade_count_5s"] is None and result["trade_covered_5s"] == 0.
    ctx.flow.observed_at_ms = NOW+1
    with pytest.raises(ValueError, match="future"):
        extract(ctx)


def test_forming_transition_and_future():
    ctx = context()
    assert values(extract(ctx))["forming_known"] == 1.
    assert values(extract(replace(ctx, forming_candle=None)))["body_bps"] is None
    assert values(extract(replace(ctx, forming_candle=replace(ctx.forming_candle, observed_at_ms=NOW-1))))["forming_known"] == 0.
    with pytest.raises(ValueError, match="future"):
        extract(replace(ctx, forming_candle=replace(ctx.forming_candle, observed_at_ms=NOW+1)))


def test_missing_context_groups_remain_missing():
    ctx = replace(context(), flow=None, liquidity=None, structure=None, forming_candle=None)
    result = values(extract(ctx))
    assert all(result[name] is None for name in ("cvd_usd_5s", "body_bps", "wall_distance_bps", "support_distance_bps"))


@pytest.mark.parametrize("field,value", [("trade_count", -1), ("trade_count", 1.5), ("ofi_event_count", -1),
    ("trade_imbalance", 1.1), ("cvd_usd", float("nan")), ("trade_notional_usd", float("inf")), ("seconds", 4)])
def test_invalid_observed_flow_rejected(field, value):
    ctx = context()
    ctx.flow.horizons[5] = replace(ctx.flow.horizons[5], **{field: value})
    with pytest.raises(ValueError):
        extract(ctx)


@pytest.mark.parametrize("kwargs", [{"trade_windows": [5]}, {"trade_windows": (5, 5)}, {"trade_windows": (1,)},
    {"trade_windows": (True,)}, {"quote_units_verified": 1}, {"liquidity_known": "yes"}])
def test_coverage_contract(kwargs):
    with pytest.raises(ValueError):
        ContextCoverage(**kwargs)


def test_uncovered_upstream_ofi_zero_does_not_become_data():
    ctx = context()
    ctx.flow.horizons[5] = replace(ctx.flow.horizons[5], normalized_ofi=0., ofi_event_count=0)
    assert values(extract(ctx, c=replace(covered(), ofi_windows=())))["normalized_ofi_5s"] is None
    assert values(extract(ctx))["normalized_ofi_5s"] == 0.


def test_future_mutation_cannot_change_saved_past_snapshot():
    ctx = context()
    past = extract(ctx)
    ctx.flow.horizons.clear()
    ctx = replace(ctx, observed_at_ms=NOW+1000)
    assert ctx.observed_at_ms != past.ref.market_time_ms
    assert past.ref.market_time_ms == NOW and values(past)["trade_count_5s"] == 5.
