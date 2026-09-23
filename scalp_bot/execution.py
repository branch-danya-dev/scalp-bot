from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Settings
from .domain import Side


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    name: str
    entry: str = "taker_market"
    target_exit: str = "maker_limit"
    stop_exit: str = "taker_market"
    partial_exit: str = "taker_market"
    passive_entry_eligible: bool = False

    def public(self) -> dict:
        return asdict(self)


_PROFILES = {
    "trend_structure": ExecutionProfile(
        name="trend_confirmation",
        partial_exit="maker_limit",
        passive_entry_eligible=False,
    ),
    "level_breakout": ExecutionProfile(
        name="breakout_confirmation",
        partial_exit="maker_limit",
        passive_entry_eligible=False,
    ),
    "weak_level_rejection": ExecutionProfile(
        name="rejection_confirmation",
        partial_exit="maker_limit",
        passive_entry_eligible=True,
    ),
    "orderbook_density": ExecutionProfile(
        name="density_confirmation",
        partial_exit="maker_limit",
        passive_entry_eligible=True,
    ),
}


def execution_profile(strategy: str) -> ExecutionProfile:
    return _PROFILES.get(
        strategy,
        ExecutionProfile(name="default_market"),
    )


def fee_rate(config: Settings, mode: str) -> float:
    return (
        config.maker_fee_rate
        if mode == "maker_limit"
        else config.taker_fee_rate
    )


def slippage_rate(config: Settings, mode: str) -> float:
    return (
        0.0
        if mode == "maker_limit"
        else config.slippage_bps / 10_000
    )


def preferred_entry_mode(
    config: Settings,
    strategy: str,
) -> str:
    profile = execution_profile(strategy)
    if config.passive_entry_enabled and profile.passive_entry_eligible:
        return "maker_limit"
    return profile.entry



def apply_entry_slippage(
    price: float,
    side: Side,
    rate: float,
) -> float:
    resolved = max(0.0, float(rate))
    if price <= 0 or resolved <= 0:
        return price
    return price * (
        1 + resolved
        if side == Side.LONG
        else 1 - resolved
    )


def apply_exit_slippage(
    price: float,
    side: Side,
    rate: float,
) -> float:
    resolved = max(0.0, float(rate))
    if price <= 0 or resolved <= 0:
        return price
    return price * (
        1 - resolved
        if side == Side.LONG
        else 1 + resolved
    )
