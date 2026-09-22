from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .domain import OrderBook, Side, StrategyDecision, TradePlan


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
        book: OrderBook,
        available_notional: float,
        available_risk_usd: float,
        *,
        setup_id: str | None = None,
    ) -> RiskResult:
        if not decision.tradeable or decision.side is None:
            return RiskResult(False, "strategy decision is not tradeable")
        if not book.best_bid or not book.best_ask:
            return RiskResult(False, "order book is not ready")

        side = decision.side
        setup_entry = float(decision.entry)
        market_entry = float(book.executable_entry(side) or 0)
        stop = float(decision.stop)
        target = float(decision.target)
        if market_entry <= 0:
            return RiskResult(False, "executable market entry is unavailable")

        if side == Side.LONG:
            entry_drift = (market_entry - setup_entry) / setup_entry
            if market_entry <= stop:
                return RiskResult(False, "setup invalidated before entry")
            target_pct = (target - market_entry) / market_entry
        else:
            entry_drift = (setup_entry - market_entry) / setup_entry
            if market_entry >= stop:
                return RiskResult(False, "setup invalidated before entry")
            target_pct = (market_entry - target) / market_entry

        max_drift = self.config.max_entry_drift_bps / 10_000
        if entry_drift > max_drift:
            return RiskResult(
                False,
                f"setup expired: entry drift {entry_drift * 10_000:.1f} bps > {self.config.max_entry_drift_bps:.1f} bps",
            )

        stop_pct = abs(market_entry - stop) / market_entry
        if stop_pct <= 0 or target_pct <= 0:
            return RiskResult(False, "invalid stop or target distance")

        risk_budget = min(balance * self.config.risk_fraction, max(available_risk_usd, 0))
        if risk_budget <= 0:
            return RiskResult(False, "portfolio risk budget exhausted")
        notional_by_risk = risk_budget / stop_pct
        portfolio_exposure_cap = balance * self.config.max_leverage
        position_leverage_cap = (
            balance * max(0.0, self.config.max_position_leverage)
        )
        position_share_cap = (
            portfolio_exposure_cap
            * max(0.0, self.config.max_position_exposure_fraction)
        )
        position_exposure_cap = min(
            position_leverage_cap,
            position_share_cap,
        )
        notional = min(
            notional_by_risk,
            max(available_notional, 0),
            position_exposure_cap,
        )
        if notional <= 0:
            return RiskResult(False, "portfolio exposure budget exhausted")

        fee_cost = notional * self.config.taker_fee_rate * 2
        slippage_cost = notional * (self.config.slippage_bps / 10_000) * 2
        spread_cost = notional * max(book.spread_pct, 0)
        estimated_costs = fee_cost + slippage_cost + spread_cost
        gross_profit = notional * target_pct
        gross_loss = notional * stop_pct
        expected_net = gross_profit - estimated_costs
        expected_net_loss = gross_loss + estimated_costs
        net_rr = expected_net / expected_net_loss if expected_net_loss > 0 else 0.0

        if expected_net <= 0:
            return RiskResult(
                False,
                f"net at target ${expected_net:.2f} <= 0 after estimated trading costs",
            )
        if expected_net < self.config.min_net_profit_usd:
            return RiskResult(
                False,
                f"net at target ${expected_net:.2f} < research minimum ${self.config.min_net_profit_usd:.2f}",
            )
        if (
            self.config.enforce_net_reward_risk_gate
            and net_rr < self.config.min_net_reward_risk
        ):
            return RiskResult(
                False,
                f"net reward/risk {net_rr:.2f} < minimum {self.config.min_net_reward_risk:.2f}",
            )

        resolved_setup_id = setup_id or decision.setup_id or f"{decision.strategy}:{side.value}:{setup_entry:.10g}"
        plan = TradePlan(
            symbol=symbol,
            strategy=decision.strategy,
            side=side,
            setup_entry=setup_entry,
            market_entry=market_entry,
            stop=stop,
            target=target,
            notional=notional,
            leverage=notional / balance if balance else 0,
            max_loss_usd=gross_loss,
            expected_gross_profit=gross_profit,
            estimated_costs=estimated_costs,
            expected_net_profit=expected_net,
            expected_net_loss=expected_net_loss,
            net_reward_risk=net_rr,
            entry_drift_pct=entry_drift,
            setup_id=resolved_setup_id,
            strategy_details=dict(decision.details),
        )
        return RiskResult(True, "allowed", plan)
