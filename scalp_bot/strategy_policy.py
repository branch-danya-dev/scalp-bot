from __future__ import annotations

from .config import Settings


def minimum_expectancy_r(
    config: Settings,
    strategy: str,
) -> float:
    return {
        "trend_structure": config.trend_structure_min_expectancy_r,
        "weak_level_rejection": config.weak_level_rejection_min_expectancy_r,
        "orderbook_density": config.density_min_expectancy_r,
        "level_breakout": config.breakout_min_expectancy_r,
    }.get(strategy, 0.0)


def partial_take_fraction(
    config: Settings,
    strategy: str,
) -> float:
    value = {
        "trend_structure": config.trend_structure_partial_take_fraction,
        "weak_level_rejection": config.weak_level_rejection_partial_take_fraction,
        "orderbook_density": config.density_partial_take_fraction,
        "level_breakout": config.breakout_partial_take_fraction,
    }.get(strategy, config.partial_take_fraction)
    return max(0.0, min(float(value), 1.0))


def no_follow_through_seconds(
    config: Settings,
    strategy: str,
) -> float:
    return max(0.0, float({
        "trend_structure": config.trend_structure_no_follow_through_seconds,
        "weak_level_rejection": config.weak_level_rejection_no_follow_through_seconds,
        "orderbook_density": config.density_no_follow_through_seconds,
        "level_breakout": config.breakout_no_follow_through_seconds,
    }.get(strategy, config.no_follow_through_seconds)))
