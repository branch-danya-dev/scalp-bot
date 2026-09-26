"""Pure M1b1 adapter: existing MarketContext -> versioned immutable features.

No alternative scanner, price indicator pipeline or model is introduced here.
Offline and future online producers must construct the same MarketContext and
prove source coverage. Coverage is explicit; zeros never stand in for missing
feeds. A structurally valid snapshot is NOT training or order authorization.
"""
from dataclasses import dataclass
import hashlib
import json
from math import isfinite

from .contracts import FeatureSnapshot, SnapshotRef
from ..strategy.market_context import MarketContext


@dataclass(frozen=True, slots=True)
class ContextCoverage:
    trade_windows: tuple[int, ...] = ()
    ofi_windows: tuple[int, ...] = ()
    forming_known: bool = False
    liquidity_known: bool = False
    structure_known: bool = False
    quote_units_verified: bool = False

    def __post_init__(self):
        for values in (self.trade_windows, self.ofi_windows):
            if type(values) is not tuple or any(type(n) is not int or n not in (5, 15, 60) for n in values) or len(set(values)) != len(values):
                raise ValueError("coverage windows must be unique supported seconds in a tuple")
        for name in ("forming_known", "liquidity_known", "structure_known", "quote_units_verified"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("coverage flags must be booleans")


# (name, unit); extraction order is part of the schema, not inferred from a dict.
_FIELDS = [
    ("book_ready", "boolean"), ("candle_fresh", "boolean"),
    ("book_age_seconds", "seconds"), ("spread_bps", "bps"),
    ("top5_depth_usd", "USD"), ("top5_depth_imbalance", "ratio"),
]
for _seconds in (5, 15, 60):
    _FIELDS.extend((f"{name}_{_seconds}s", unit) for name, unit in (
        ("trade_covered", "boolean"), ("trade_count", "count"),
        ("trade_notional_usd", "USD"), ("cvd_usd", "USD"), ("trade_imbalance", "ratio"),
        ("ofi_covered", "boolean"), ("ofi_event_count", "count"), ("normalized_ofi", "ratio")))
_FIELDS.extend([
    ("forming_known", "boolean"), ("body_bps", "bps"), ("range_bps", "bps"),
    ("close_position", "ratio"), ("volume_pace_ratio", "ratio"),
    ("range_expansion_ratio", "ratio"), ("velocity_bps_per_second", "bps/second"),
    ("micro_move_5s_bps", "bps"), ("micro_move_15s_bps", "bps"),
    ("liquidity_known", "boolean"), ("wall_present", "boolean"),
    ("wall_distance_bps", "bps"), ("remaining_ratio", "ratio"),
    ("attack_ratio", "ratio"), ("depletion_per_second", "1/second"),
    ("replenishment_ratio", "ratio"), ("structure_known", "boolean"),
    ("support_distance_bps", "bps"), ("resistance_distance_bps", "bps"),
])
FEATURE_FIELDS = tuple(_FIELDS)
FEATURE_NAMES = tuple(name for name, _ in FEATURE_FIELDS)
_SCHEMA = dict(version=1, fields=FEATURE_FIELDS, missing="None; explicit coverage masks",
    normalization="upstream MarketContext, no fit or future samples",
    freshness="book ready+synced; same-time flow/forming; explicitly covered structure/liquidity",
    monetary_units="USD only with verified linear contract/quote units")
FEATURE_SCHEMA = "market-context-v1:" + hashlib.sha256(
    json.dumps(_SCHEMA, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def schema_description() -> dict:
    """Return a fresh JSON-compatible schema without loading data or a model."""
    return json.loads(json.dumps(_SCHEMA)) | {"schema_id": FEATURE_SCHEMA}


def _number(value, name, *, nonnegative=False):
    if value is None:
        return None
    if type(value) not in (int, float) or not isfinite(value) or (nonnegative and value < 0):
        raise ValueError(f"invalid numeric feature {name}")
    return float(value)


def extract_context_features(context: MarketContext, ref: SnapshotRef, coverage: ContextCoverage) -> FeatureSnapshot:
    """Freeze one causal context. Caller proves per-feed coverage at ref.sequence.

    Source sequence/clock provenance cannot be proved from aggregated context
    alone. Liquidity/structure have no own timestamp; explicit flags are required.
    """
    if not isinstance(context, MarketContext) or not isinstance(coverage, ContextCoverage):
        raise ValueError("typed MarketContext and ContextCoverage required")
    if ref.feature_schema != FEATURE_SCHEMA or ref.symbol != context.symbol or ref.market_time_ms != context.observed_at_ms:
        raise ValueError("feature schema, symbol or observation time mismatch")
    values = {name: None for name in FEATURE_NAMES}
    execution = context.execution
    quote = bool(execution.ready and execution.book_synced is True and execution.best_bid is not None
                 and execution.best_ask is not None and 0 < execution.best_bid < execution.best_ask)
    values.update(book_ready=float(quote), candle_fresh=float(execution.candle_fresh),
                  book_age_seconds=_number(execution.book_age_seconds, "book_age_seconds", nonnegative=True))
    if quote:
        spread = _number(execution.spread_pct, "spread", nonnegative=True)
        values["spread_bps"] = None if spread is None else spread * 10000
        bid = _number(execution.top5_bid_notional_usd, "bid_depth", nonnegative=True)
        ask = _number(execution.top5_ask_notional_usd, "ask_depth", nonnegative=True)
        if bid is not None and ask is not None and bid + ask > 0 and coverage.quote_units_verified:
            values["top5_depth_usd"] = bid + ask
            values["top5_depth_imbalance"] = (bid - ask) / (bid + ask)
    flow = context.flow
    if flow is not None and flow.observed_at_ms > ref.market_time_ms:
        raise ValueError("flow contains future observation")
    same_time_flow = flow is not None and flow.observed_at_ms == ref.market_time_ms
    for seconds in (5, 15, 60):
        horizon = flow.horizons.get(seconds) if same_time_flow else None
        if horizon is not None and horizon.seconds != seconds:
            raise ValueError("flow horizon unit mismatch")
        trades_known = horizon is not None and seconds in coverage.trade_windows
        ofi_known = horizon is not None and seconds in coverage.ofi_windows and quote
        values[f"trade_covered_{seconds}s"] = float(trades_known)
        values[f"ofi_covered_{seconds}s"] = float(ofi_known)
        if trades_known:
            count = _number(horizon.trade_count, "trade_count", nonnegative=True)
            if count is None or not count.is_integer():
                raise ValueError("trade count must be an integer")
            values[f"trade_count_{seconds}s"] = count
            if coverage.quote_units_verified:
                values[f"trade_notional_usd_{seconds}s"] = _number(horizon.trade_notional_usd, "notional", nonnegative=True)
                values[f"cvd_usd_{seconds}s"] = _number(horizon.cvd_usd, "cvd")
            if count > 0:
                imbalance = _number(horizon.trade_imbalance, "imbalance")
                if imbalance is None or not -1 <= imbalance <= 1:
                    raise ValueError("trade imbalance outside [-1,1]")
                values[f"trade_imbalance_{seconds}s"] = imbalance
        if ofi_known:
            count = _number(horizon.ofi_event_count, "ofi_count", nonnegative=True)
            if count is None or not count.is_integer():
                raise ValueError("OFI event count must be an integer")
            values[f"ofi_event_count_{seconds}s"] = count
            if coverage.quote_units_verified and values["top5_depth_usd"] is not None:
                values[f"normalized_ofi_{seconds}s"] = _number(horizon.normalized_ofi, "normalized_ofi")
    forming = context.forming_candle
    if forming is not None and (forming.observed_at_ms > ref.market_time_ms or forming.start_ms > ref.market_time_ms):
        raise ValueError("forming candle contains future observation")
    forming_known = bool(coverage.forming_known and execution.candle_fresh and forming is not None
                         and forming.observed_at_ms == ref.market_time_ms)
    values["forming_known"] = float(forming_known)
    if forming_known:
        for destination, attr, scale in (
            ("body_bps", "body_pct", 10000), ("range_bps", "range_pct", 10000),
            ("close_position", "close_position", 1), ("volume_pace_ratio", "volume_pace_ratio", 1),
            ("range_expansion_ratio", "range_expansion_ratio", 1),
            ("velocity_bps_per_second", "velocity_bps_per_second", 1)):
            value = _number(getattr(forming, attr), destination)
            values[destination] = None if value is None else value * scale
        for seconds in (5, 15):
            if seconds in coverage.trade_windows:
                name = f"micro_move_{seconds}s_bps"
                values[name] = _number(getattr(forming, name), name)
    liquidity = context.liquidity
    liquidity_known = bool(coverage.liquidity_known and quote and liquidity is not None
                           and liquidity.state.value != "unknown")
    values["liquidity_known"] = float(liquidity_known)
    if liquidity_known:
        if liquidity.wall_present is not None:
            if type(liquidity.wall_present) is not bool:
                raise ValueError("wall_present must be bool or None")
            values["wall_present"] = float(liquidity.wall_present)
        for name in ("remaining_ratio", "attack_ratio", "depletion_per_second", "replenishment_ratio"):
            values[name] = _number(getattr(liquidity, name), name, nonnegative=True)
        distance = _number(liquidity.distance_pct, "wall distance", nonnegative=True)
        values["wall_distance_bps"] = None if distance is None else distance * 10000
    structure_known = bool(coverage.structure_known and execution.candle_fresh and context.structure is not None)
    values["structure_known"] = float(structure_known)
    if structure_known:
        for side in ("support", "resistance"):
            value = _number(getattr(context.structure, f"{side}_distance_pct"), side, nonnegative=True)
            values[f"{side}_distance_bps"] = None if value is None else value * 10000
    return FeatureSnapshot(ref, FEATURE_NAMES, tuple(values[name] for name in FEATURE_NAMES))
