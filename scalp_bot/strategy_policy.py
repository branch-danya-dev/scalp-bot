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
