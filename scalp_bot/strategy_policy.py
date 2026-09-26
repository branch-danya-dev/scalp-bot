from __future__ import annotations

from math import isfinite

from .config import Settings
from .domain import Side


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
        "price_action_hypothesis": config.breakout_no_follow_through_seconds,
    }.get(strategy, config.no_follow_through_seconds)))


def breakout_impulse_limit(
    strategy: str, side: Side, setup_entry: float, details: dict,
) -> tuple[float | None, str | None]:
    """Freeze a first-take ceiling in price space; never renew it at a later fill."""
    if strategy != "level_breakout":
        return None, None
    trigger = details.get("opportunityTrigger")
    anchors = []
    if isinstance(trigger, dict):
        anchors.append((trigger.get("price"), trigger.get("expectedImpulsePct"), "price_episode"))
    anchors.append((setup_entry, details.get("expectedImpulsePct"), "signal_impulse"))
    limits = []
    direction = 1 if side == Side.LONG else -1
    for anchor, impulse, source in anchors:
        if (type(anchor) in (int, float) and type(impulse) in (int, float)
                and isfinite(anchor) and isfinite(impulse) and anchor > 0 and 0 < impulse < 1):
            limits.append((anchor * (1 + direction * impulse), source))
    if not limits:
        return None, None  # Older/manual decisions without an impulse keep their policy.
    return min(limits, key=lambda row: direction * row[0])
