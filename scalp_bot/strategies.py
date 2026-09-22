"""Compatibility exports.

Strategy implementations live in scalp_bot.strategy, one strategy per module.
New code should import from scalp_bot.strategy directly.
"""

from .strategy import (
    DEFAULT_STRATEGIES,
    BreakoutStage,
    DensityBounceStrategy,
    DensityStage,
    LevelBreakoutStrategy,
    LevelZone,
    RejectionStage,
    Strategy,
    TrendStructureStrategy,
    WeakLevelRejectionStrategy,
    classify_trend,
    compute_trade_flow,
    detect_level_zones,
)

__all__ = [
    "Strategy",
    "LevelZone",
    "classify_trend",
    "compute_trade_flow",
    "detect_level_zones",
    "TrendStructureStrategy",
    "WeakLevelRejectionStrategy",
    "DensityBounceStrategy",
    "LevelBreakoutStrategy",
    "RejectionStage",
    "DensityStage",
    "BreakoutStage",
    "DEFAULT_STRATEGIES",
]
