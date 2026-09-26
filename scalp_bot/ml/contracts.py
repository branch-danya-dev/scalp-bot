"""Immutable messages for a future local predictor, not order instructions.

Monotonic values may be compared only inside the same clock_domain. The producer
must freeze features after applying source_sequence; this module cannot verify
raw-market provenance, executable depth or calibration of model probabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _uint(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    capture_id: str
    symbol: str
    selection_epoch: int
    source_sequence: int
    market_time_ms: int
    available_mono_ns: int
    clock_domain: str
    feature_schema: str

    def __post_init__(self) -> None:
        for name in ("capture_id", "symbol", "clock_domain", "feature_schema"):
            _text(getattr(self, name), name)
        for name in ("selection_epoch", "source_sequence", "market_time_ms", "available_mono_ns"):
            _uint(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    ref: SnapshotRef
    names: tuple[str, ...]
    values: tuple[float | None, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SnapshotRef):
            raise ValueError("ref must be SnapshotRef")
        # Tuples prohibit aliasing a mutable list shared with the live engine.
        if type(self.names) is not tuple or type(self.values) is not tuple:
            raise ValueError("feature names and values must be tuples")
        if not self.names or len(self.names) != len(self.values):
            raise ValueError("feature names and values must have equal nonzero lengths")
        for name in self.names:
            _text(name, "feature name")
        if len(set(self.names)) != len(self.names):
            raise ValueError("duplicate feature names")
        for value in self.values:
            if value is not None and (type(value) not in (int, float) or not isfinite(value)):
                raise ValueError("features must be finite numbers or explicit None")


@dataclass(frozen=True, slots=True)
class ImpulseForecast:
    source: SnapshotRef
    model_version: str
    plan_policy_version: str
    side: str
    horizon_ms: int
    produced_mono_ns: int
    expires_mono_ns: int
    p_target_first: float
    p_stop_first: float
    p_timeout: float

    def __post_init__(self) -> None:
        if not isinstance(self.source, SnapshotRef):
            raise ValueError("source must be SnapshotRef")
        _text(self.model_version, "model_version")
        _text(self.plan_policy_version, "plan_policy_version")
        if self.side not in ("long", "short"):
            raise ValueError("side must be long or short")
        for name in ("horizon_ms", "produced_mono_ns", "expires_mono_ns"):
            _uint(getattr(self, name), name)
        if self.horizon_ms == 0:
            raise ValueError("horizon_ms must be positive")
        if not self.source.available_mono_ns <= self.produced_mono_ns < self.expires_mono_ns:
            raise ValueError("forecast timestamps are inconsistent")
        probabilities = (self.p_target_first, self.p_stop_first, self.p_timeout)
        if any(type(p) not in (int, float) or not isfinite(p) or not 0 <= p <= 1 for p in probabilities):
            raise ValueError("invalid probability")
        if not isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("outcome probabilities must sum to one")


def forecast_rejections(
    forecast: ImpulseForecast,
    current: SnapshotRef,
    *,
    now_mono_ns: int,
    max_data_age_ns: int,
    model_version: str,
    plan_policy_version: str,
) -> tuple[str, ...]:
    """Metadata/freshness check ONLY; an empty result is NOT permission to trade.

    The caller must additionally verify data health, current price, model-card
    approval, matching features/plan, economics, ownership and portfolio risk.
    """
    _uint(now_mono_ns, "now_mono_ns")
    _uint(max_data_age_ns, "max_data_age_ns")
    _text(model_version, "model_version")
    _text(plan_policy_version, "plan_policy_version")
    if max_data_age_ns == 0:
        raise ValueError("max_data_age_ns must be positive")
    reasons = []
    for field in ("capture_id", "symbol", "selection_epoch", "clock_domain", "feature_schema"):
        if getattr(forecast.source, field) != getattr(current, field):
            reasons.append(f"{field}_mismatch")
    if forecast.model_version != model_version:
        reasons.append("model_version_mismatch")
    if forecast.plan_policy_version != plan_policy_version:
        reasons.append("plan_policy_version_mismatch")
    if forecast.source.source_sequence > current.source_sequence:
        reasons.append("future_source_sequence")
    if forecast.source.clock_domain == current.clock_domain:
        if current.available_mono_ns > now_mono_ns or forecast.produced_mono_ns > now_mono_ns:
            reasons.append("future_timestamp")
        if forecast.source.available_mono_ns > current.available_mono_ns:
            reasons.append("source_newer_than_current")
        if now_mono_ns >= forecast.expires_mono_ns:
            reasons.append("forecast_expired")
        if now_mono_ns - forecast.source.available_mono_ns >= max_data_age_ns:
            reasons.append("source_data_stale")
    return tuple(reasons)
