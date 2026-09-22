from __future__ import annotations

from math import log10

from .domain import Candidate, Candle


def _returns_by_start(candles: list[Candle]) -> dict[int, float]:
    result: dict[int, float] = {}
    previous: Candle | None = None
    for candle in candles:
        if previous is not None and previous.close > 0:
            result[candle.start_ms] = candle.close / previous.close - 1.0
        previous = candle
    return result


def correlation_1h(candles: list[Candle], benchmark: list[Candle]) -> float | None:
    """Pearson correlation of aligned 1m returns over the available ~1h window."""
    left = _returns_by_start(candles[-61:])
    right = _returns_by_start(benchmark[-61:])
    keys = sorted(set(left) & set(right))
    if len(keys) < 20:
        return None

    xs = [left[key] for key in keys]
    ys = [right[key] for key in keys]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    var_x = sum(x * x for x in dx)
    var_y = sum(y * y for y in dy)
    if var_x <= 0 or var_y <= 0:
        return None
    value = sum(x * y for x, y in zip(dx, dy)) / (var_x * var_y) ** 0.5
    return max(-1.0, min(1.0, value))


def activity_score(candidate: Candidate, window_minutes: int) -> float:
    turnover = max(candidate.turnover_24h, 0.0)
    turnover_component = min(
        1.0,
        max(0.0, log10(1 + turnover / 50_000_000) / log10(21)),
    )
    move_24h = min(1.0, abs(candidate.change_24h) / 0.25)
    move_recent = min(1.0, abs(candidate.activity_change) / 0.02)

    expected_recent = turnover * max(window_minutes, 1) / 1440
    burst_ratio = (
        candidate.activity_turnover / expected_recent
        if expected_recent > 0
        else 0.0
    )
    burst = min(1.0, burst_ratio / 3.0)

    # Correlation is kept as neutral context. Until paper data proves that
    # independence from BTC is itself predictive, it must not mechanically
    # increase or decrease a coin's activity score.
    return 100.0 * (
        turnover_component * 0.20
        + move_24h * 0.30
        + move_recent * 0.25
        + burst * 0.25
    )
