from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import Settings
from .domain import Side


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    symbol: str
    maker_fee_rate: float
    taker_fee_rate: float
    source: str = "configured"

    def public(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_public(
        cls,
        payload: dict[str, Any] | None,
    ) -> "FeeSchedule | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                symbol=str(payload.get("symbol") or ""),
                maker_fee_rate=float(
                    payload.get("maker_fee_rate")
                    if "maker_fee_rate" in payload
                    else payload.get("makerFeeRate")
                ),
                taker_fee_rate=float(
                    payload.get("taker_fee_rate")
                    if "taker_fee_rate" in payload
                    else payload.get("takerFeeRate")
                ),
                source=str(payload.get("source") or "unknown"),
            )
        except (TypeError, ValueError):
            return None


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
    "price_action_hypothesis": ExecutionProfile(name="candle_hypothesis_beta"),
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
        # Production rejection waits for a causal micro-response before FIRE.
        # Enter immediately once confirmed; waiting for a passive retrace would
        # contradict the signal and systematically miss the rejection move.
        passive_entry_eligible=False,
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


def fee_rate(
    config: Settings,
    mode: str,
    schedule: FeeSchedule | None = None,
) -> float:
    if schedule is not None:
        return (
            schedule.maker_fee_rate
            if mode == "maker_limit"
            else schedule.taker_fee_rate
        )
    return (
        config.maker_fee_rate
        if mode == "maker_limit"
        else config.taker_fee_rate
    )


def fee_rate_for_details(
    config: Settings,
    mode: str,
    details: dict[str, Any] | None,
) -> float:
    raw = (
        details.get("feeSchedule")
        if isinstance(details, dict)
        else None
    )
    schedule = FeeSchedule.from_public(
        raw if isinstance(raw, dict) else None
    )
    return fee_rate(config, mode, schedule)


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
