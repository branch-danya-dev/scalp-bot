from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .domain import StrategyDecision, TradePlan


@dataclass(slots=True)
class RiskResult:
    allowed: bool
    reason: str
    plan: TradePlan | None = None


class RiskEngine:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def build_plan(
        self,
        symbol: str,
        decision: StrategyDecision,
        balance: float,
        spread_pct: float,
    ) -> RiskResult:
        if not decision.tradeable or decision.side is None:
            return RiskResult(False, "strategy decision is not tradeable")
        entry = float(decision.entry)
        stop = float(decision.stop)
        target = float(decision.target)
        stop_pct = abs(entry - stop) / entry
        target_pct = abs(target - entry) / entry
        if stop_pct <= 0 or target_pct <= 0:
            return RiskResult(False, "invalid stop or target distance")

        max_loss = balance * self.config.risk_fraction
        notional_by_risk = max_loss / stop_pct
        notional_cap = balance * self.config.max_leverage
        notional = min(notional_by_risk, notional_cap)
        if notional <= 0:
            return RiskResult(False, "position size is zero")

        fee_cost = notional * self.config.taker_fee_rate * 2
        slippage_cost = notional * (self.config.slippage_bps / 10_000) * 2
        spread_cost = notional * max(spread_pct, 0)
        estimated_costs = fee_cost + slippage_cost + spread_cost
        gross_profit = notional * target_pct
        expected_net = gross_profit - estimated_costs

        if expected_net < self.config.min_net_profit_usd:
            return RiskResult(
                False,
                f"expected net ${expected_net:.2f} < minimum ${self.config.min_net_profit_usd:.2f}",
            )

        plan = TradePlan(
            symbol=symbol,
            strategy=decision.strategy,
            side=decision.side,
            entry=entry,
            stop=stop,
            target=target,
            notional=notional,
            leverage=notional / balance if balance else 0,
            max_loss_usd=notional * stop_pct,
            expected_gross_profit=gross_profit,
            estimated_costs=estimated_costs,
            expected_net_profit=expected_net,
        )
        return RiskResult(True, "allowed", plan)
