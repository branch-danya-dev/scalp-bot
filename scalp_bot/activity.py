from __future__ import annotations

from math import log10
from statistics import median

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


def _level_proximity_score(
    candles: list[Candle],
    *,
    baseline_range_pct: float,
) -> float:
    if len(candles) < 12 or candles[-1].close <= 0:
        return 0.0

    window = candles[-40:]
    pivots: list[float] = []
    span = 2
    for index in range(span, len(window) - span):
        row = window[index]
        left = window[index - span:index]
        right = window[index + 1:index + span + 1]
        if (
            row.high >= max(item.high for item in left)
            and row.high >= max(item.high for item in right)
        ):
            pivots.append(row.high)
        if (
            row.low <= min(item.low for item in left)
            and row.low <= min(item.low for item in right)
        ):
            pivots.append(row.low)

    if len(pivots) < 2:
        return 0.0

    current = candles[-1].close
    cluster_tolerance = max(
        baseline_range_pct * 0.75,
        0.0005,
    )
    clusters: list[list[float]] = []
    for price in pivots:
        match = next(
            (
                cluster
                for cluster in clusters
                if abs(
                    price
                    - sum(cluster) / len(cluster)
                )
                / max(
                    sum(cluster) / len(cluster),
                    1e-9,
                )
                <= cluster_tolerance
            ),
            None,
        )
        if match is None:
            clusters.append([price])
        else:
            match.append(price)

    significant = [
        sum(cluster) / len(cluster)
        for cluster in clusters
        if len(cluster) >= 2
    ]
    if not significant:
        return 0.0

    distance = min(
        abs(current - level) / current
        for level in significant
    )
    approach_window = max(
        0.004,
        baseline_range_pct * 3.0,
    )
    return max(
        0.0,
        min(1.0, 1.0 - distance / approach_window),
    )


def opportunity_readiness(
    candles: list[Candle],
) -> tuple[float, float, float, float, float]:
    """Estimate pre-opportunity state without predicting a direction.

    The scanner should prefer markets transitioning from compression into
    fresh expansion, not simply the symbols that have already travelled the
    farthest over the recent window.
    """
    confirmed = [row for row in candles if row.confirmed]
    if len(confirmed) < 12:
        return 0.0, 1.0, 1.0, 0.0, 0.0

    def range_pct(row: Candle) -> float:
        return (
            (row.high - row.low) / row.close
            if row.close > 0 and row.high >= row.low
            else 0.0
        )

    ranges = [
        value
        for value in (
            range_pct(row)
            for row in confirmed[-40:]
        )
        if value > 0
    ]
    if len(ranges) < 10:
        return 0.0, 1.0, 1.0, 0.0, 0.0

    recent_rows = confirmed[-7:]
    compression_rows = recent_rows[:5]
    expansion_rows = recent_rows[-2:]
    baseline_rows = confirmed[
        max(0, len(confirmed) - 37):
        max(0, len(confirmed) - 7)
    ]
    baseline_ranges = [
        range_pct(row)
        for row in baseline_rows
        if range_pct(row) > 0
    ]
    baseline = median(
        baseline_ranges or ranges[:-2] or ranges
    )
    compression = median([
        range_pct(row)
        for row in compression_rows
        if range_pct(row) > 0
    ] or [baseline])
    expansion = median([
        range_pct(row)
        for row in expansion_rows
        if range_pct(row) > 0
    ] or [baseline])

    compression_ratio = (
        compression / baseline
        if baseline > 0
        else 1.0
    )
    expansion_ratio = (
        expansion / baseline
        if baseline > 0
        else 1.0
    )

    move_start = recent_rows[0].open
    move_end = recent_rows[-1].close
    recent_move = (
        abs(move_end - move_start) / move_start
        if move_start > 0
        else 0.0
    )
    expected_impulse = max(
        0.0015,
        baseline * 2.4,
    )
    move_spent_ratio = (
        recent_move / expected_impulse
        if expected_impulse > 0
        else 0.0
    )

    compression_score = max(
        0.0,
        min(1.0, (1.05 - compression_ratio) / 0.45),
    )
    expansion_score = max(
        0.0,
        min(1.0, (expansion_ratio - 0.85) / 0.75),
    )
    unspent_score = max(
        0.0,
        min(
            1.0,
            1.0 - max(0.0, move_spent_ratio - 0.35) / 1.0,
        ),
    )
    level_proximity_score = _level_proximity_score(
        confirmed,
        baseline_range_pct=baseline,
    )
    readiness = 100.0 * (
        compression_score * 0.30
        + expansion_score * 0.25
        + unspent_score * 0.25
        + level_proximity_score * 0.20
    )
    return (
        readiness,
        compression_ratio,
        expansion_ratio,
        move_spent_ratio,
        level_proximity_score,
    )


def activity_score(candidate: Candidate, window_minutes: int) -> float:
    turnover = max(candidate.turnover_24h, 0.0)
    turnover_component = min(
        1.0,
        max(0.0, log10(1 + turnover / 50_000_000) / log10(21)),
    )
    move_24h = min(1.0, abs(candidate.change_24h) / 0.25)
    move_recent = min(1.0, abs(candidate.activity_change) / 0.02)

    expected_recent = turnover * max(window_minutes, 1) / 1440
    burst_ratio = candidate.activity_burst_ratio
    if burst_ratio <= 0:
        burst_ratio = (
            candidate.activity_turnover / expected_recent
            if expected_recent > 0
            else 0.0
        )
    burst = min(1.0, burst_ratio / 3.0)

    readiness = max(
        0.0,
        min(candidate.opportunity_readiness / 100.0, 1.0),
    )

    # Correlation remains context-only. Stage 19C reduces pure chase bias:
    # recent absolute move is only a small attention component, while a fresh
    # compression->expansion transition receives the largest weight.
    return 100.0 * (
        turnover_component * 0.15
        + move_24h * 0.15
        + move_recent * 0.10
        + burst * 0.25
        + readiness * 0.35
    )
