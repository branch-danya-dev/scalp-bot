"""Exchange-time bounds anchored to a monotonic clock.

The caller must sample server time DURING the supplied request interval.
This component never infers clock offset from market trades or advances time to
the newest trade. Receipt freshness and event availability remain caller duties.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ClockReading:
    local_wall_ms: float
    monotonic_seconds: float
    exchange_lower_ms: float | None
    exchange_upper_ms: float | None
    uncertainty_ms: float | None
    sync_age_seconds: float | None
    valid: bool
    reason: str | None

    @property
    def evaluation_ms(self) -> int | None:
        # Use the conservative upper bound ONLY on already received/processed
        # events. It is never permission to reveal later events from a batch.
        if not self.valid or self.exchange_upper_ms is None:
            return None
        return math.ceil(self.exchange_upper_ms)


class MarketClock:
    def __init__(self, *, max_rtt_ms: float = 400, max_sync_age_seconds: float = 60,
                 max_uncertainty_ms: float = 250, wall_jump_ms: float = 250,
                 drift_ppm: float = 50, timestamp_precision_ms: float = 1) -> None:
        values = (max_rtt_ms, max_sync_age_seconds, max_uncertainty_ms, wall_jump_ms,
                  drift_ppm, timestamp_precision_ms)
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("clock limits must be finite and nonnegative")
        self.max_rtt_ms = max_rtt_ms
        self.max_sync_age_seconds = max_sync_age_seconds
        self.max_uncertainty_ms = max_uncertainty_ms
        self.wall_jump_ms = wall_jump_ms
        self.drift_ppm = drift_ppm
        self.precision_ms = timestamp_precision_ms
        self._anchor: tuple[float, float, float, float] | None = None
        self._last: tuple[float, float] | None = None
        self._last_evaluation_ms: int | None = None
        self._fault: str | None = "unsynchronized"
        self.last_sync_rejection: str | None = None
        self.last_sync_upper_padding_ms: float = 0.0

    def synchronize(self, *, server_ms: float, sent_mono: float,
                    received_mono: float, received_wall_ms: float) -> bool:
        self.last_sync_rejection = None
        self.last_sync_upper_padding_ms = 0.0
        values = (server_ms, sent_mono, received_mono, received_wall_ms)
        if any(not math.isfinite(v) for v in values) or server_ms <= 0 or received_mono < sent_mono:
            self._fault = self.last_sync_rejection = "invalid_sync_sample"
            return False
        rtt_ms = (received_mono - sent_mono) * 1000
        if rtt_ms > self.max_rtt_ms:
            self.last_sync_rejection = "sync_rtt_exceeded"
            if self._anchor is None:
                self._fault = self.last_sync_rejection
            return False
        lower = server_ms - self.precision_ms
        upper = server_ms + rtt_ms + self.precision_ms
        if (upper - lower) / 2 > self.max_uncertainty_ms:
            self.last_sync_rejection = "clock_uncertainty_exceeded"
            if self._anchor is None:
                self._fault = self.last_sync_rejection
            return False
        if self._last is not None and received_mono < self._last[0]:
            self._fault = self.last_sync_rejection = "monotonic_rollback"
            return False
        if self._last_evaluation_ms is not None and math.ceil(upper) < self._last_evaluation_ms:
            # A smaller RTT can narrow an overlapping interval below the last
            # evaluation upper bound without the exchange clock going back.
            # Retain that watermark conservatively, within the same uncertainty
            # budget. Never use a market event timestamp to move these bounds.
            overlaps = False
            if self._anchor is not None:
                anchor_mono, _, old_lower, old_upper = self._anchor
                age = received_mono - anchor_mono
                drift = max(0.0, age) * self.drift_ppm / 1000
                overlaps = (0 <= age <= self.max_sync_age_seconds
                            and lower <= old_upper + age * 1000 + drift
                            and upper >= old_lower + age * 1000 - drift)
            if not overlaps:
                self._fault = self.last_sync_rejection = "exchange_clock_rollback"
                return False
            padded_upper = float(self._last_evaluation_ms)
            if (padded_upper - lower) / 2 > self.max_uncertainty_ms:
                self.last_sync_rejection = "clock_uncertainty_exceeded"
                return False
            self.last_sync_upper_padding_ms = padded_upper - upper
            upper = padded_upper
        self._anchor = received_mono, received_wall_ms, lower, upper
        self._last = received_mono, received_wall_ms
        self._fault = None
        self.last_sync_rejection = None
        return True

    def read(self, *, mono: float, wall_ms: float,
             latest_processed_event_ms: float | None = None) -> ClockReading:
        if not math.isfinite(mono) or not math.isfinite(wall_ms):
            raise ValueError("clock observations must be finite")
        if self._last is not None:
            previous_mono, previous_wall = self._last
            elapsed_ms = (mono - previous_mono) * 1000
            if elapsed_ms < 0:
                self._fault = "monotonic_rollback"
            elif abs(wall_ms - previous_wall - elapsed_ms) > self.wall_jump_ms:
                self._fault = "local_wall_jump"
        self._last = mono, wall_ms
        if self._anchor is None:
            return ClockReading(wall_ms, mono, None, None, None, None, False, self._fault)
        anchor_mono, anchor_wall, lower, upper = self._anchor
        age = mono - anchor_mono
        drift_ms = max(0.0, age) * self.drift_ppm / 1000
        lower += age * 1000 - drift_ms
        upper += age * 1000 + drift_ms
        uncertainty = (upper - lower) / 2
        reason = self._fault
        if age < 0:
            reason = "monotonic_rollback"
        elif age > self.max_sync_age_seconds:
            reason = reason or "synchronization_expired"
        elif uncertainty > self.max_uncertainty_ms:
            reason = reason or "clock_uncertainty_exceeded"
        elif abs(wall_ms - anchor_wall - age * 1000) > self.wall_jump_ms:
            reason = reason or "local_wall_drift"
        if latest_processed_event_ms is not None:
            if not math.isfinite(latest_processed_event_ms) or latest_processed_event_ms > upper:
                reason = reason or "event_outside_clock_bound"
        if self._last_evaluation_ms is not None and math.ceil(upper) < self._last_evaluation_ms:
            reason = reason or "exchange_clock_rollback"
        reading = ClockReading(wall_ms, mono, lower, upper, uncertainty, age, reason is None, reason)
        if reading.valid:
            self._last_evaluation_ms = reading.evaluation_ms
        return reading


def receipt_age_seconds(*, now_mono: float, received_mono: float) -> float | None:
    """Unknown/invalid receipt age must fail freshness gates, never become zero."""
    if not math.isfinite(now_mono) or not math.isfinite(received_mono) or now_mono < received_mono:
        return None
    return now_mono - received_mono
