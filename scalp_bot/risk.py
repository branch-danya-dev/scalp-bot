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

        risk_budget = min(
            balance * self.config.risk_fraction,
            max(available_risk_usd, 0),
        )
        if risk_budget <= 0:
            return RiskResult(False, "portfolio risk budget exhausted")
        notional_by_risk = risk_budget / stop_pct

        round_trip_cost_pct = (
            self.config.taker_fee_rate * 2
            + (self.config.slippage_bps / 10_000) * 2
        )
        all_in_loss_pct = stop_pct + round_trip_cost_pct
        notional_by_all_in_portfolio_risk = (
            max(available_risk_usd, 0) / all_in_loss_pct
            if all_in_loss_pct > 0
            else 0.0
        )
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
            notional_by_all_in_portfolio_risk,
            max(available_notional, 0),
            position_exposure_cap,
        )
        if notional <= 0:
            return RiskResult(False, "portfolio exposure budget exhausted")

        fee_cost = notional * self.config.taker_fee_rate * 2
        slippage_cost = (
            notional
            * (self.config.slippage_bps / 10_000)
            * 2
        )
        # Entry already uses the executable ask/bid and paper exits are
        # triggered on the executable opposite side. Subtracting a second
        # full spread here would double-count the same friction.
        estimated_costs = fee_cost + slippage_cost
        gross_profit = notional * target_pct
        gross_loss = notional * stop_pct
        expected_net = gross_profit - estimated_costs
        all_in_net_loss = gross_loss + estimated_costs
        # Keep expected_net_loss as the internal/public compatibility alias.
        # The payoff gate uses the explicit all-in loss amount so fees and
        # slippage are counted exactly once on both target and stop outcomes.
        expected_net_loss = all_in_net_loss
        net_rr = expected_net / all_in_net_loss if all_in_net_loss > 0 else 0.0

        required_net_profit = max(
            self.config.min_net_profit_usd,
            balance * max(
                0.0,
                self.config.min_net_profit_equity_fraction,
            ),
        )

        if expected_net <= 0:
            return RiskResult(
                False,
                f"net at target ${expected_net:.2f} <= 0 after estimated trading costs",
            )
        if expected_net < required_net_profit:
            return RiskResult(
                False,
                (
                    f"net at target ${expected_net:.2f} < required "
                    f"${required_net_profit:.2f}"
                ),
            )
        minimum_net_reward = (
            all_in_net_loss * self.config.min_net_reward_risk
        )
        if (
            self.config.enforce_net_reward_risk_gate
            and expected_net < minimum_net_reward
        ):
            return RiskResult(
                False,
                (
                    "economic_gate: insufficient_net_reward_risk: "
                    f"net reward/risk {net_rr:.4f} < minimum "
                    f"{self.config.min_net_reward_risk:.4f}"
                ),
            )

        economics = {
            "riskBudgetUsd": risk_budget,
            "notionalByRiskUsd": notional_by_risk,
            "notionalByAllInPortfolioRiskUsd": (
                notional_by_all_in_portfolio_risk
            ),
            "effectiveLeverage": notional / balance if balance else 0.0,
            "stopDistancePct": stop_pct,
            "roundTripCostPct": round_trip_cost_pct,
            "allInLossPct": all_in_loss_pct,
            "targetMovePct": target_pct,
            "takerFeeCostUsd": fee_cost,
            "slippageCostUsd": slippage_cost,
            "estimatedCostsUsd": estimated_costs,
            "entrySpreadPct": max(book.spread_pct, 0.0),
            "spreadCostDoubleCounted": False,
            "grossAtTargetUsd": gross_profit,
            "netAtTargetUsd": expected_net,
            "netAtStopUsd": expected_net_loss,
            "allInNetLossUsd": all_in_net_loss,
            "netReturnOnEquity": (
                expected_net / balance if balance > 0 else 0.0
            ),
            "netRewardRisk": net_rr,
            "requiredNetRewardRisk": self.config.min_net_reward_risk,
            "netRewardRiskRatio": net_rr,
            "minimumNetRewardRiskRatio": self.config.min_net_reward_risk,
            "payoffGateEnabled": (
                self.config.enforce_net_reward_risk_gate
            ),
            "payoffMarginUsd": expected_net - minimum_net_reward,
            "requiredNetProfitUsd": required_net_profit,
            "requiredNetProfitEquityFraction": (
                self.config.min_net_profit_equity_fraction
            ),
        }
        strategy_details = dict(decision.details)
        strategy_details["economics"] = economics

        resolved_setup_id = (
            setup_id
            or decision.setup_id
            or f"{decision.strategy}:{side.value}:{setup_entry:.10g}"
        )
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
            strategy_details=strategy_details,
        )
        return RiskResult(True, "allowed", plan)
