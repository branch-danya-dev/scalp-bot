from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .domain import OrderBook, Side, StrategyDecision, TradePlan
from .execution import execution_profile, fee_rate, slippage_rate
from .strategy_policy import partial_take_fraction


@dataclass(slots=True)
class RiskResult:
    allowed: bool
    reason: str
    plan: TradePlan | None = None
    diagnostics: dict | None = None


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

        structural_risk_budget = balance * self.config.risk_fraction
        trade_all_in_cap_usd = (
            balance * self.config.max_trade_all_in_loss_fraction
        )
        if structural_risk_budget <= 0:
            return RiskResult(False, "structural risk budget exhausted")
        if trade_all_in_cap_usd <= 0:
            return RiskResult(False, "trade all-in loss cap exhausted")
        if available_risk_usd <= 0:
            return RiskResult(False, "portfolio risk budget exhausted")

        execution = execution_profile(decision.strategy)
        entry_fee_rate = fee_rate(self.config, execution.entry)
        target_exit_fee_rate = fee_rate(
            self.config,
            execution.target_exit,
        )
        stop_exit_fee_rate = fee_rate(
            self.config,
            execution.stop_exit,
        )
        partial_exit_fee_rate = fee_rate(
            self.config,
            execution.partial_exit,
        )
        entry_slippage_rate = slippage_rate(
            self.config,
            execution.entry,
        )
        target_exit_slippage_rate = slippage_rate(
            self.config,
            execution.target_exit,
        )
        stop_exit_slippage_rate = slippage_rate(
            self.config,
            execution.stop_exit,
        )
        partial_exit_slippage_rate = slippage_rate(
            self.config,
            execution.partial_exit,
        )
        target_cost_pct = (
            entry_fee_rate
            + target_exit_fee_rate
            + entry_slippage_rate
            + target_exit_slippage_rate
        )
        stop_cost_pct = (
            entry_fee_rate
            + stop_exit_fee_rate
            + entry_slippage_rate
            + stop_exit_slippage_rate
        )
        round_trip_cost_pct = stop_cost_pct
        all_in_loss_pct = stop_pct + stop_cost_pct
        # Size from the strategy invalidation distance first. Costs are not
        # allowed to silently shrink structural risk; they are constrained by
        # a separate per-trade all-in loss cap and the aggregate portfolio cap.
        notional_by_structural_risk = structural_risk_budget / stop_pct
        notional_by_trade_all_in_cap = (
            trade_all_in_cap_usd / all_in_loss_pct
            if all_in_loss_pct > 0
            else 0.0
        )
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
            notional_by_structural_risk,
            notional_by_trade_all_in_cap,
            notional_by_all_in_portfolio_risk,
            max(available_notional, 0),
            position_exposure_cap,
        )
        if notional <= 0:
            return RiskResult(False, "portfolio exposure budget exhausted")

        best_entry = market_entry
        depth_entry, visible_entry_depth = book.entry_vwap(
            side,
            notional,
        )
        if (
            depth_entry is None
            or visible_entry_depth + max(1e-9, notional * 1e-9)
            < notional
        ):
            return RiskResult(
                False,
                (
                    "insufficient visible entry depth: "
                    f"{visible_entry_depth:.2f} < {notional:.2f} USD"
                ),
            )

        market_entry = depth_entry
        if side == Side.LONG:
            entry_drift = (market_entry - setup_entry) / setup_entry
            if market_entry <= stop:
                return RiskResult(False, "setup invalidated by depth-adjusted entry")
            target_pct = (target - market_entry) / market_entry
        else:
            entry_drift = (setup_entry - market_entry) / setup_entry
            if market_entry >= stop:
                return RiskResult(False, "setup invalidated by depth-adjusted entry")
            target_pct = (market_entry - target) / market_entry
        if entry_drift > max_drift:
            return RiskResult(
                False,
                (
                    "setup expired after depth: entry drift "
                    f"{entry_drift * 10_000:.1f} bps > "
                    f"{self.config.max_entry_drift_bps:.1f} bps"
                ),
            )

        stop_pct = abs(market_entry - stop) / market_entry
        if stop_pct <= 0 or target_pct <= 0:
            return RiskResult(
                False,
                "invalid stop or target distance after depth adjustment",
            )

        all_in_loss_pct = stop_pct + round_trip_cost_pct
        notional_by_structural_risk = structural_risk_budget / stop_pct
        notional_by_trade_all_in_cap = (
            trade_all_in_cap_usd / all_in_loss_pct
            if all_in_loss_pct > 0
            else 0.0
        )
        notional_by_all_in_portfolio_risk = (
            max(available_risk_usd, 0) / all_in_loss_pct
            if all_in_loss_pct > 0
            else 0.0
        )
        depth_sized_notional = min(
            notional_by_structural_risk,
            notional_by_trade_all_in_cap,
            notional_by_all_in_portfolio_risk,
            max(available_notional, 0),
            position_exposure_cap,
        )
        if depth_sized_notional < notional:
            notional = depth_sized_notional
            depth_entry, visible_entry_depth = book.entry_vwap(
                side,
                notional,
            )
            if (
                depth_entry is None
                or visible_entry_depth + max(1e-9, notional * 1e-9)
                < notional
            ):
                return RiskResult(
                    False,
                    "insufficient visible entry depth after risk sizing",
                )
            market_entry = depth_entry
            if side == Side.LONG:
                entry_drift = (market_entry - setup_entry) / setup_entry
                target_pct = (target - market_entry) / market_entry
            else:
                entry_drift = (setup_entry - market_entry) / setup_entry
                target_pct = (market_entry - target) / market_entry
            stop_pct = abs(market_entry - stop) / market_entry
            all_in_loss_pct = stop_pct + round_trip_cost_pct

        entry_depth_impact_bps = (
            max(0.0, (market_entry - best_entry) / best_entry * 10_000)
            if side == Side.LONG
            else max(0.0, (best_entry - market_entry) / best_entry * 10_000)
        )

        target_fee_cost = notional * (
            entry_fee_rate + target_exit_fee_rate
        )
        target_slippage_cost = notional * (
            entry_slippage_rate + target_exit_slippage_rate
        )
        stop_fee_cost = notional * (
            entry_fee_rate + stop_exit_fee_rate
        )
        stop_slippage_cost = notional * (
            entry_slippage_rate + stop_exit_slippage_rate
        )
        stop_estimated_costs = stop_fee_cost + stop_slippage_cost

        # Price the same lifecycle that paper execution will actually use.
        # Known runner strategies can realize a partial at 1R and then carry
        # only the remainder to the final/runner target. The old model priced
        # 100% of notional at the final maker target and materially overstated
        # winners such as the UNI regression from the 2026-09-22 run.
        profile_is_known = decision.strategy in {
            "trend_structure",
            "weak_level_rejection",
            "orderbook_density",
            "level_breakout",
        }
        allow_runner = bool(decision.details.get("allowRunner", True))
        partial_fraction = partial_take_fraction(
            self.config,
            decision.strategy,
        )
        partial_move_pct = stop_pct * max(
            0.0,
            self.config.partial_take_at_r,
        )
        partial_enabled = (
            profile_is_known
            and self.config.partial_take_enabled
            and allow_runner
            and 0.0 < partial_fraction < 1.0
            and target_pct > partial_move_pct
        )
        target_source = str(decision.details.get("targetSource") or "")
        structural_liquidity_target = (
            target_source == "liquidity"
            or isinstance(decision.details.get("liquidityTarget"), dict)
        )
        runner_target_pct = target_pct
        if partial_enabled and not structural_liquidity_target:
            runner_target_pct = max(
                target_pct,
                stop_pct * max(0.0, self.config.runner_target_r),
            )

        if partial_enabled:
            runner_fraction = 1.0 - partial_fraction
            lifecycle_gross_pct = (
                partial_fraction * partial_move_pct
                + runner_fraction * runner_target_pct
            )
            lifecycle_fee_pct = (
                entry_fee_rate
                + partial_fraction * partial_exit_fee_rate
                + runner_fraction * target_exit_fee_rate
            )
            lifecycle_slippage_pct = (
                entry_slippage_rate
                + partial_fraction * partial_exit_slippage_rate
                + runner_fraction * target_exit_slippage_rate
            )
        else:
            runner_fraction = 1.0
            lifecycle_gross_pct = target_pct
            lifecycle_fee_pct = entry_fee_rate + target_exit_fee_rate
            lifecycle_slippage_pct = (
                entry_slippage_rate + target_exit_slippage_rate
            )

        lifecycle_cost_pct = lifecycle_fee_pct + lifecycle_slippage_pct
        lifecycle_fee_cost = notional * lifecycle_fee_pct
        lifecycle_slippage_cost = notional * lifecycle_slippage_pct
        estimated_costs = lifecycle_fee_cost + lifecycle_slippage_cost
        fee_cost = lifecycle_fee_cost
        slippage_cost = lifecycle_slippage_cost
        gross_profit = notional * lifecycle_gross_pct
        gross_loss = notional * stop_pct
        expected_net = gross_profit - estimated_costs
        all_in_net_loss = gross_loss + stop_estimated_costs
        winner_cost_share = (
            estimated_costs / gross_profit
            if gross_profit > 0
            else float("inf")
        )
        stop_cost_share = (
            stop_estimated_costs / gross_loss
            if gross_loss > 0
            else float("inf")
        )
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
        minimum_net_reward = (
            all_in_net_loss * self.config.min_net_reward_risk
        )
        minimum_net_profit_failed = expected_net < required_net_profit
        net_reward_risk_failed = expected_net < minimum_net_reward

        economic_diagnostics = {
            "riskBudgetUsd": structural_risk_budget,
            "structuralRiskBudgetUsd": structural_risk_budget,
            "tradeAllInLossCapUsd": trade_all_in_cap_usd,
            "riskSizingBasis": "structural_stop_with_all_in_cap",
            "notionalByRiskUsd": notional_by_structural_risk,
            "notionalByStructuralRiskUsd": notional_by_structural_risk,
            "notionalByTradeAllInCapUsd": notional_by_trade_all_in_cap,
            "notionalByAllInPortfolioRiskUsd": notional_by_all_in_portfolio_risk,
            "effectiveLeverage": notional / balance if balance else 0.0,
            "setupEntry": setup_entry,
            "marketEntry": market_entry,
            "stop": stop,
            "target": target,
            "stopDistancePct": stop_pct,
            "targetMovePct": target_pct,
            "targetCostPct": lifecycle_cost_pct,
            "fullTargetCostPct": target_cost_pct,
            "stopCostPct": stop_cost_pct,
            "winnerCostShare": winner_cost_share,
            "maximumWinnerCostShare": self.config.max_winner_cost_share,
            "winnerCostShareGateEnabled": self.config.enforce_winner_cost_share_gate,
            "stopCostShare": stop_cost_share,
            "maximumStopCostShare": self.config.max_stop_cost_share,
            "stopCostShareGateEnabled": self.config.enforce_stop_cost_share_gate,
            "partialFraction": partial_fraction if partial_enabled else 0.0,
            "partialMovePct": partial_move_pct if partial_enabled else None,
            "runnerFraction": runner_fraction if partial_enabled else 1.0,
            "runnerTargetPct": runner_target_pct,
            "lifecycleGrossPct": lifecycle_gross_pct,
            "lifecycleCostPct": lifecycle_cost_pct,
            "partialExitFeeRate": partial_exit_fee_rate,
            "partialExitSlippageRate": partial_exit_slippage_rate,
            "executionProfile": execution.public(),
            "entryFeeRate": entry_fee_rate,
            "targetExitFeeRate": target_exit_fee_rate,
            "stopExitFeeRate": stop_exit_fee_rate,
            "entrySlippageRate": entry_slippage_rate,
            "targetExitSlippageRate": target_exit_slippage_rate,
            "stopExitSlippageRate": stop_exit_slippage_rate,
            "allInLossPct": all_in_loss_pct,
            "targetEstimatedCostsUsd": estimated_costs,
            "stopEstimatedCostsUsd": stop_estimated_costs,
            "entrySpreadPct": max(book.spread_pct, 0.0),
            "entryDepthImpactBps": entry_depth_impact_bps,
            "visibleEntryDepthUsd": visible_entry_depth,
            "grossAtTargetUsd": gross_profit,
            "grossLifecycleUsd": gross_profit,
            "legacyGrossAtFinalTargetUsd": notional * target_pct,
            "structuralLossAtStopUsd": gross_loss,
            "plannedAllInLossUsd": all_in_net_loss,
            "netAtTargetUsd": expected_net,
            "netAtStopUsd": expected_net_loss,
            "allInNetLossUsd": all_in_net_loss,
            "netReturnOnEquity": expected_net / balance if balance > 0 else 0.0,
            "netRewardRisk": net_rr,
            "requiredNetRewardRisk": self.config.min_net_reward_risk,
            "minimumNetProfitGateEnabled": self.config.enforce_min_net_profit_gate,
            "payoffGateEnabled": self.config.enforce_net_reward_risk_gate,
            "requiredNetProfitUsd": required_net_profit,
            "requiredNetProfitEquityFraction": self.config.min_net_profit_equity_fraction,
            "wouldFailMinimumNetProfit": minimum_net_profit_failed,
            "wouldFailNetRewardRisk": net_reward_risk_failed,
        }

        if (
            self.config.enforce_winner_cost_share_gate
            and winner_cost_share > self.config.max_winner_cost_share
        ):
            return RiskResult(
                False,
                (
                    "economic_gate: winner_cost_share "
                    f"{winner_cost_share:.3f} > maximum "
                    f"{self.config.max_winner_cost_share:.3f}"
                ),
                diagnostics=dict(economic_diagnostics),
            )
        if (
            self.config.enforce_stop_cost_share_gate
            and stop_cost_share > self.config.max_stop_cost_share
        ):
            return RiskResult(
                False,
                (
                    "economic_gate: stop_cost_share "
                    f"{stop_cost_share:.3f} > maximum "
                    f"{self.config.max_stop_cost_share:.3f}"
                ),
                diagnostics=dict(economic_diagnostics),
            )

        if expected_net <= 0:
            return RiskResult(
                False,
                f"net at target ${expected_net:.2f} <= 0 after estimated trading costs",
                diagnostics=dict(economic_diagnostics),
            )
        if self.config.enforce_min_net_profit_gate and minimum_net_profit_failed:
            return RiskResult(
                False,
                (
                    f"net at target ${expected_net:.2f} < required "
                    f"${required_net_profit:.2f}"
                ),
                diagnostics=dict(economic_diagnostics),
            )
        if self.config.enforce_net_reward_risk_gate and net_reward_risk_failed:
            return RiskResult(
                False,
                (
                    "economic_gate: insufficient_net_reward_risk: "
                    f"net reward/risk {net_rr:.4f} < minimum "
                    f"{self.config.min_net_reward_risk:.4f}"
                ),
                diagnostics=dict(economic_diagnostics),
            )

        shadow_reject_reasons: list[str] = []
        if minimum_net_profit_failed:
            shadow_reject_reasons.append("minimum_net_profit")
        if net_reward_risk_failed:
            shadow_reject_reasons.append("minimum_net_reward_risk")

        economics = {
            **economic_diagnostics,
            "roundTripCostPct": stop_cost_pct,
            "lifecycleCostPct": lifecycle_cost_pct,
            "winnerCostShare": winner_cost_share,
            "stopCostShare": stop_cost_share,
            "takerFeeCostUsd": fee_cost,
            "slippageCostUsd": slippage_cost,
            "estimatedCostsUsd": estimated_costs,
            "spreadCostDoubleCounted": False,
            "netRewardRiskRatio": net_rr,
            "minimumNetRewardRiskRatio": self.config.min_net_reward_risk,
            "shadowRejectReasons": shadow_reject_reasons,
            "economicPolicy": (
                "strict"
                if (
                    self.config.enforce_min_net_profit_gate
                    and self.config.enforce_net_reward_risk_gate
                )
                else "research_shadow"
            ),
            "payoffMarginUsd": expected_net - minimum_net_reward,
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
            max_loss_usd=all_in_net_loss,
            expected_gross_profit=gross_profit,
            estimated_costs=estimated_costs,
            expected_net_profit=expected_net,
            expected_net_loss=expected_net_loss,
            net_reward_risk=net_rr,
            entry_drift_pct=entry_drift,
            setup_id=resolved_setup_id,
            strategy_details=strategy_details,
        )
        return RiskResult(
            True,
            "allowed",
            plan,
            diagnostics=dict(economics),
        )
