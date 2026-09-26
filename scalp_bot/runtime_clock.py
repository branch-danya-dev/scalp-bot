"""Runtime observation clocks; exchange-time estimation remains in MarketClock."""
from __future__ import annotations

import math
import time
from typing import Protocol


class ClockTapeMismatch(ValueError):
    pass


class RecordingRuntimeClock:
    """Observe return values, without using the decorated clock in the sink."""
    def __init__(self, source: RuntimeClock, observe) -> None:
        self.source = source
        self.observe = observe

    def _read(self, method):
        value = getattr(self.source, method)()
        self.observe(method, value)
        return value

    def time(self) -> float:
        return self._read("time")

    def monotonic(self) -> float:
        return self._read("monotonic")

    def perf_counter_ns(self) -> int:
        return self._read("perf_counter_ns")


class ClockTape:
    """Strict clock-call playback, with no OS fallback or implicit time advance.

    The caller selects observations for a processing scope in recorded order.
    A mismatch keeps the offending observation pending for diagnostics.
    """
    def __init__(self, observations) -> None:
        self._rows = iter(observations)
        self._empty = object()
        self._end = object()
        self._pending = self._empty

    def _peek(self):
        if self._pending is self._empty:
            self._pending = next(self._rows, self._end)
        return self._pending

    def _read(self, method):
        row = self._peek()
        if not isinstance(row, dict) or row.get("method") != method:
            raise ClockTapeMismatch(f"unexpected or missing clock call: {method}")
        value = row.get("value")
        try:
            valid = (type(value) is int and value >= 0 if method == "perf_counter_ns" else
                     type(value) in (int, float) and math.isfinite(value))
        except OverflowError:
            valid = False
        if not valid:
            raise ClockTapeMismatch("invalid recorded clock value")
        self._pending = self._empty
        return value

    def time(self) -> float:
        return self._read("time")

    def monotonic(self) -> float:
        return self._read("monotonic")

    def perf_counter_ns(self) -> int:
        return self._read("perf_counter_ns")

    def assert_exhausted(self) -> None:
        if self._peek() is not self._end:
            raise ClockTapeMismatch("unconsumed clock observations")


class RuntimeClock(Protocol):
    def time(self) -> float: ...
    def monotonic(self) -> float: ...
    def perf_counter_ns(self) -> int: ...


class SystemRuntimeClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def perf_counter_ns(self) -> int:
        return time.perf_counter_ns()


class ReplayRuntimeClock:
    """Explicit observations, never sleeps or reads OS clocks.

    Wall time may jump in either direction to reproduce recorded faults.
    The replay driver must order monotonic observations, including equal times.
    An inconsistent ordering fails before changing either observation.
    """

    def __init__(self, *, wall_seconds: float, mono_ns: int) -> None:
        self._validate(wall_seconds, mono_ns)
        self._wall_seconds = wall_seconds
        self._mono_ns = mono_ns

    @staticmethod
    def _validate(wall_seconds: float, mono_ns: int) -> None:
        if not math.isfinite(wall_seconds):
            raise ValueError("wall observation must be finite")
        if type(mono_ns) is not int or mono_ns < 0:
            raise ValueError("monotonic observation must be nonnegative integer nanoseconds")

    def set_observation(self, *, wall_seconds: float, mono_ns: int) -> None:
        self._validate(wall_seconds, mono_ns)
        if mono_ns < self._mono_ns:
            raise ValueError("replay observations are out of monotonic order")
        self._wall_seconds = wall_seconds
        self._mono_ns = mono_ns

    def time(self) -> float:
        return self._wall_seconds

    def monotonic(self) -> float:
        return self._mono_ns / 1e9

    def perf_counter_ns(self) -> int:
        return self._mono_ns
