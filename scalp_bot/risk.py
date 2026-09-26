from __future__ import annotations

from .execution_book import coherent_execution_book

from dataclasses import dataclass

from .config import Settings
from .domain import OrderBook, Side, StrategyDecision, TradePlan
from .instrument import InstrumentSpec
from .execution import (
    apply_entry_slippage,
    apply_exit_slippage,
    execution_profile,
    FeeSchedule,
    fee_rate,
    preferred_entry_mode,
    slippage_rate,
)
from .strategy_policy import breakout_impulse_limit, partial_take_fraction


@dataclass(slots=True)
class RiskResult:
    allowed: bool
    reason: str
    plan: TradePlan | None = None
    diagnostics: dict | None = None


class RiskEngine:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def _stop_depth_stress(
        self,
        book: OrderBook,
        side: Side,
        notional: float,
        trigger_price: float,
    ) -> tuple[float, float, float, float | None, str]:
        """Estimate adverse stop execution without assuming impossible depth.

        Prefer book levels already visible beyond the planned stop trigger.
        Most normal books will not expose useful quantity that far away; in
        that case project the *current depth impact* onto the future stop
        price instead of rejecting every otherwise-valid setup.
        """
        if notional <= 0 or trigger_price <= 0:
            return 0.0, 0.0, 0.0, None, "unavailable"

        raw_vwap, visible = book.exit_vwap_from_trigger(
            side,
            notional,
            trigger_price,
        )
        if raw_vwap is not None and raw_vwap > 0 and visible > 0:
            impact = (
                max(
                    0.0,
                    (trigger_price - raw_vwap)
                    / trigger_price,
                )
                if side == Side.LONG
                else max(
                    0.0,
                    (raw_vwap - trigger_price)
                    / trigger_price,
                )
            )
            model = "visible_levels_beyond_stop_trigger"
        else:
            best = book.executable_exit(side)
            current_vwap, visible = book.exit_vwap(
                side,
                notional,
            )
            if (
                best is None
                or best <= 0
                or current_vwap is None
                or current_vwap <= 0
                or visible <= 0
            ):
                return 0.0, 0.0, visible, None, "unavailable"

            impact = (
                max(0.0, (best - current_vwap) / best)
                if side == Side.LONG
                else max(0.0, (current_vwap - best) / best)
            )
            raw_vwap = (
                trigger_price * (1 - impact)
                if side == Side.LONG
                else trigger_price * (1 + impact)
            )
            model = "current_depth_impact_projected_to_stop"

        stress = impact * max(
            0.0,
            self.config.stop_depth_stress_multiplier,
        )
        return stress, impact, visible, raw_vwap, model

    def build_plan(
        self,
        symbol: str,
        decision: StrategyDecision,
        balance: float,
        book: OrderBook,
        available_notional: float,
        available_risk_usd: float,
        *,
        depth_book: OrderBook | None = None,
        instrument: InstrumentSpec | None = None,
        fee_schedule: FeeSchedule | None = None,
        setup_id: str | None = None,
        existing_position_notional: float = 0.0,
        existing_position_all_in_risk_usd: float = 0.0,
    ) -> RiskResult:
        if not decision.tradeable or decision.side is None:
            return RiskResult(False, "strategy decision is not tradeable")
        if not book.best_bid or not book.best_ask:
            return RiskResult(False, "fast order book is not ready")
        depth = coherent_execution_book(book, depth_book)
        if not depth.best_bid or not depth.best_ask:
            return RiskResult(False, "deep order book is not ready")

        side = decision.side
        if instrument is not None and not instrument.tradeable:
            return RiskResult(
                False,
                f"instrument is not tradeable: {instrument.status}",
            )
        setup_entry = float(decision.entry)
        execution = execution_profile(decision.strategy)
        entry_mode = preferred_entry_mode(
            self.config,
            decision.strategy,
        )
        entry_slippage_rate = slippage_rate(
            self.config,
            entry_mode,
        )
        if entry_mode == "maker_limit":
            raw_market_entry = float(
                min(setup_entry, book.best_bid)
                if side == Side.LONG
                else max(setup_entry, book.best_ask)
            )
            if instrument is not None:
                raw_market_entry = instrument.maker_entry_price(
                    raw_market_entry,
                    side,
                )
        else:
            raw_market_entry = float(
                book.executable_entry(side) or 0
            )
        market_entry = apply_entry_slippage(
            raw_market_entry,
            side,
            entry_slippage_rate,
        )
        best_raw_entry = raw_market_entry
        stop = float(decision.stop)
        target = float(decision.target)
        if instrument is not None:
            stop = instrument.stop_price(stop, side)
            target = instrument.target_price(target, side)
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

        requested_risk_scale = (
            (decision.details or {}).get("riskScale", 1.0)
        )
        risk_scale = (
            float(requested_risk_scale)
            if isinstance(requested_risk_scale, (int, float))
            else 1.0
        )
        # Stage 19B allows semantic evidence to de-risk a setup, but never to
        # lever it above the configured base risk until positive expectancy is
        # proven from paper data.
        risk_scale = max(0.25, min(risk_scale, 1.0))
        staged_entry = (
            (decision.details or {}).get("stagedEntry")
            if isinstance(
                (decision.details or {}).get("stagedEntry"),
                dict,
            )
            else {}
        )
        requested_entry_risk_fraction = staged_entry.get(
            "riskFraction",
            1.0,
        )
        entry_risk_fraction = (
            float(requested_entry_risk_fraction)
            if isinstance(
                requested_entry_risk_fraction,
                (int, float),
            )
            else 1.0
        )
        entry_risk_fraction = max(
            0.05,
            min(entry_risk_fraction, 1.0),
        )
        base_structural_risk_budget = (
            balance * self.config.risk_fraction
        )
        scaled_structural_risk_budget = (
            base_structural_risk_budget * risk_scale
        )
        structural_risk_budget = (
            scaled_structural_risk_budget
            * entry_risk_fraction
        )
        full_trade_all_in_cap_usd = (
            balance * self.config.max_trade_all_in_loss_fraction
        )
        trade_all_in_cap_usd = max(
            0.0,
            full_trade_all_in_cap_usd
            - max(0.0, existing_position_all_in_risk_usd),
        )
        if structural_risk_budget <= 0:
            return RiskResult(False, "structural risk budget exhausted")
        if trade_all_in_cap_usd <= 0:
            return RiskResult(False, "trade all-in loss cap exhausted")
        if available_risk_usd <= 0:
            return RiskResult(False, "portfolio risk budget exhausted")

        entry_fee_rate = fee_rate(
            self.config,
            entry_mode,
            fee_schedule,
        )
        target_exit_fee_rate = fee_rate(
            self.config,
            execution.target_exit,
            fee_schedule,
        )
        stop_exit_fee_rate = fee_rate(
            self.config,
            execution.stop_exit,
            fee_schedule,
        )
        partial_exit_fee_rate = fee_rate(
            self.config,
            execution.partial_exit,
            fee_schedule,
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
            + target_exit_slippage_rate
        )
        stop_cost_pct = (
            entry_fee_rate
            + stop_exit_fee_rate
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
        full_position_exposure_cap = min(
            position_leverage_cap,
            position_share_cap,
        )
        position_exposure_cap = max(
            0.0,
            full_position_exposure_cap
            - max(0.0, existing_position_notional),
        )
        initial_risk_notional = notional_by_structural_risk / (risk_scale * entry_risk_fraction)
        notional = min(
            notional_by_structural_risk,
            notional_by_trade_all_in_cap,
            notional_by_all_in_portfolio_risk,
            max(available_notional, 0),
            position_exposure_cap,
        )
        if notional <= 0:
            return RiskResult(False, "portfolio exposure budget exhausted")

        if entry_mode == "maker_limit":
            raw_depth_entry = raw_market_entry
            visible_entry_depth = notional
        else:
            raw_depth_entry, visible_entry_depth = (
                depth.entry_vwap(
                    side,
                    notional,
                )
            )
            if (
                raw_depth_entry is None
                or visible_entry_depth
                + max(1e-9, notional * 1e-9)
                < notional
            ):
                return RiskResult(
                    False,
                    (
                        "insufficient visible entry depth: "
                        f"{visible_entry_depth:.2f} < {notional:.2f} USD"
                    ),
                )
        market_entry = apply_entry_slippage(
            float(raw_depth_entry),
            side,
            entry_slippage_rate,
        )
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
            if entry_mode == "maker_limit":
                raw_depth_entry = raw_market_entry
                visible_entry_depth = notional
            else:
                raw_depth_entry, visible_entry_depth = (
                    depth.entry_vwap(
                        side,
                        notional,
                    )
                )
                if (
                    raw_depth_entry is None
                    or visible_entry_depth
                    + max(1e-9, notional * 1e-9)
                    < notional
                ):
                    return RiskResult(
                        False,
                        "insufficient visible entry depth after risk sizing",
                    )
            market_entry = apply_entry_slippage(
                float(raw_depth_entry),
                side,
                entry_slippage_rate,
            )
            if side == Side.LONG:
                entry_drift = (market_entry - setup_entry) / setup_entry
                target_pct = (target - market_entry) / market_entry
            else:
                entry_drift = (setup_entry - market_entry) / setup_entry
                target_pct = (market_entry - target) / market_entry
            stop_pct = abs(market_entry - stop) / market_entry
            all_in_loss_pct = stop_pct + round_trip_cost_pct

        stop_depth_stress_rate = 0.0
        stop_depth_impact_rate = 0.0
        visible_stop_depth = 0.0
        raw_stop_exit_vwap: float | None = None
        stop_depth_model = "unavailable"
        for _ in range(3):
            (
                stop_depth_stress_rate,
                stop_depth_impact_rate,
                visible_stop_depth,
                raw_stop_exit_vwap,
                stop_depth_model,
            ) = self._stop_depth_stress(
                depth,
                side,
                notional,
                stop,
            )
            if raw_stop_exit_vwap is None or visible_stop_depth <= 0:
                return RiskResult(
                    False,
                    "insufficient visible stop-side depth",
                )

            stressed_all_in_loss_pct = (
                stop_pct
                + round_trip_cost_pct
                + stop_depth_stress_rate
            )
            notional_by_structural_risk = (
                structural_risk_budget / stop_pct
            )
            notional_by_trade_all_in_cap = (
                trade_all_in_cap_usd / stressed_all_in_loss_pct
                if stressed_all_in_loss_pct > 0
                else 0.0
            )
            notional_by_all_in_portfolio_risk = (
                max(available_risk_usd, 0)
                / stressed_all_in_loss_pct
                if stressed_all_in_loss_pct > 0
                else 0.0
            )
            stressed_notional = min(
                notional_by_structural_risk,
                notional_by_trade_all_in_cap,
                notional_by_all_in_portfolio_risk,
                max(available_notional, 0),
                position_exposure_cap,
                visible_stop_depth,
            )
            if stressed_notional <= 0:
                return RiskResult(
                    False,
                    "stop-side liquidity stress exhausts risk budget",
                )
            if stressed_notional >= notional - max(
                1e-9,
                notional * 1e-9,
            ):
                all_in_loss_pct = stressed_all_in_loss_pct
                break

            notional = stressed_notional
            if entry_mode == "maker_limit":
                raw_depth_entry = raw_market_entry
                visible_entry_depth = notional
            else:
                raw_depth_entry, visible_entry_depth = depth.entry_vwap(
                    side,
                    notional,
                )
                if (
                    raw_depth_entry is None
                    or visible_entry_depth
                    + max(1e-9, notional * 1e-9)
                    < notional
                ):
                    return RiskResult(
                        False,
                        "insufficient visible entry depth after stop stress sizing",
                    )
            market_entry = apply_entry_slippage(
                float(raw_depth_entry),
                side,
                entry_slippage_rate,
            )
            if side == Side.LONG:
                entry_drift = (
                    market_entry - setup_entry
                ) / setup_entry
                if market_entry <= stop:
                    return RiskResult(
                        False,
                        "setup invalidated after stop stress sizing",
                    )
                target_pct = (
                    target - market_entry
                ) / market_entry
            else:
                entry_drift = (
                    setup_entry - market_entry
                ) / setup_entry
                if market_entry >= stop:
                    return RiskResult(
                        False,
                        "setup invalidated after stop stress sizing",
                    )
                target_pct = (
                    market_entry - target
                ) / market_entry
            if entry_drift > max_drift:
                return RiskResult(
                    False,
                    (
                        "setup expired after stop stress sizing: entry drift "
                        f"{entry_drift * 10_000:.1f} bps > "
                        f"{self.config.max_entry_drift_bps:.1f} bps"
                    ),
                )
            stop_pct = abs(
                market_entry - stop
            ) / market_entry
            if stop_pct <= 0 or target_pct <= 0:
                return RiskResult(
                    False,
                    "invalid stop or target after stop stress sizing",
                )
        else:
            all_in_loss_pct = (
                stop_pct
                + round_trip_cost_pct
                + stop_depth_stress_rate
            )

        quantity = (
            notional / market_entry
            if market_entry > 0
            else 0.0
        )
        instrument_diagnostics = None
        if instrument is not None:
            normalized = instrument.normalize_quantity(
                entry_price=market_entry,
                requested_notional=notional,
                market_order=entry_mode != "maker_limit",
            )
            if normalized is None:
                return RiskResult(
                    False,
                    "instrument minimum/step constraints reject order size",
                )
            quantity, notional = normalized
            (
                stop_depth_stress_rate,
                stop_depth_impact_rate,
                visible_stop_depth,
                raw_stop_exit_vwap,
                stop_depth_model,
            ) = self._stop_depth_stress(
                depth,
                side,
                notional,
                stop,
            )
            if raw_stop_exit_vwap is None or visible_stop_depth <= 0:
                return RiskResult(
                    False,
                    "insufficient visible stop-side depth after quantity normalization",
                )
            all_in_loss_pct = (
                stop_pct
                + round_trip_cost_pct
                + stop_depth_stress_rate
            )
            instrument_diagnostics = {
                **instrument.public(),
                "normalizedQuantity": quantity,
                "normalizedNotionalUsd": notional,
            }

        entry_depth_impact_bps = (
            max(
                0.0,
                (
                    float(raw_depth_entry)
                    - best_raw_entry
                )
                / best_raw_entry
                * 10_000,
            )
            if side == Side.LONG
            else max(
                0.0,
                (
                    best_raw_entry
                    - float(raw_depth_entry)
                )
                / best_raw_entry
                * 10_000,
            )
        )

        direction = 1 if side == Side.LONG else -1
        entry_fee_cost = (
            quantity * market_entry * entry_fee_rate
        )
        embedded_entry_slippage_usd = (
            quantity
            * abs(
                market_entry - float(raw_depth_entry)
            )
        )

        stop_slipped_fill = apply_exit_slippage(
            stop,
            side,
            stop_exit_slippage_rate,
        )
        stop_stressed_fill = stop_slipped_fill * (
            1 - stop_depth_stress_rate
            if side == Side.LONG
            else 1 + stop_depth_stress_rate
        )
        stop_slippage_cost = (
            quantity * abs(stop_slipped_fill - stop)
        )
        stop_depth_stress_cost = (
            quantity
            * abs(
                stop_stressed_fill - stop_slipped_fill
            )
        )
        stop_exit_fee_cost = (
            quantity
            * stop_stressed_fill
            * stop_exit_fee_rate
        )
        stop_fee_cost = (
            entry_fee_cost + stop_exit_fee_cost
        )
        stop_estimated_costs = (
            stop_fee_cost
            + stop_slippage_cost
            + stop_depth_stress_cost
        )
        gross_loss = (
            quantity * abs(market_entry - stop)
        )
        all_in_net_loss = (
            gross_loss + stop_estimated_costs
        )
        all_in_loss_pct = (
            all_in_net_loss / notional
            if notional > 0
            else 0.0
        )
        stressed_stop_cost_pct = (
            stop_estimated_costs / notional
            if notional > 0
            else 0.0
        )
        stop_cost_pct = stressed_stop_cost_pct
        round_trip_cost_pct = stressed_stop_cost_pct

        full_target_fill = apply_exit_slippage(
            target,
            side,
            target_exit_slippage_rate,
        )
        target_exit_fee_cost = (
            quantity
            * full_target_fill
            * target_exit_fee_rate
        )
        target_fee_cost = (
            entry_fee_cost + target_exit_fee_cost
        )
        target_slippage_cost = (
            quantity * abs(target - full_target_fill)
        )
        target_cost_pct = (
            (
                target_fee_cost
                + target_slippage_cost
            )
            / notional
            if notional > 0
            else 0.0
        )

        required_net_profit = max(
            self.config.min_net_profit_usd,
            balance * max(
                0.0,
                self.config.min_net_profit_equity_fraction,
            ),
        )

        # Price the same quantity-based lifecycle that PaperBroker realizes.
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
        partial_requested = (
            profile_is_known
            and self.config.partial_take_enabled
            and allow_runner
            and 0.0 < partial_fraction < 1.0
        )
        partial_raw_price = (
            market_entry + direction * abs(market_entry - stop)
            * max(0.0, self.config.partial_take_at_r)
        )
        impulse_limit, impulse_limit_source = breakout_impulse_limit(
            decision.strategy, side, setup_entry, decision.details or {},
        )
        partial_capped = bool(
            partial_requested and impulse_limit is not None
            and direction * (partial_raw_price - impulse_limit) > 0
        )
        if partial_capped:
            partial_raw_price = impulse_limit
        if instrument is not None:
            partial_raw_price = instrument.target_price(partial_raw_price, side)
        partial_move_pct = direction * (partial_raw_price - market_entry) / market_entry
        partial_candidate = partial_requested and target_pct > partial_move_pct > 0
        partial_fill = apply_exit_slippage(
            partial_raw_price,
            side,
            partial_exit_slippage_rate,
        )
        partial_quantity = (
            quantity * partial_fraction
        )
        partial_raw_gross = (
            direction
            * (partial_raw_price - market_entry)
            * partial_quantity
        )
        partial_exit_fee_cost = (
            partial_quantity
            * partial_fill
            * partial_exit_fee_rate
        )
        partial_slippage_cost = (
            partial_quantity
            * abs(partial_raw_price - partial_fill)
        )
        partial_allocated_entry_fee = (
            entry_fee_cost * partial_fraction
        )
        partial_net_at_trigger_usd = (
            partial_raw_gross
            - partial_allocated_entry_fee
            - partial_exit_fee_cost
            - partial_slippage_cost
            if partial_candidate
            else 0.0
        )
        partial_leg_net_pct = (
            partial_net_at_trigger_usd
            / (
                partial_quantity * market_entry
            )
            if (
                partial_candidate
                and partial_quantity > 0
                and market_entry > 0
            )
            else 0.0
        )
        partial_required_net_usd = (
            required_net_profit
            if self.config.enforce_min_net_profit_gate
            else 0.0
        )
        partial_economic_ready = (
            partial_candidate
            and partial_net_at_trigger_usd
            >= partial_required_net_usd
            and partial_net_at_trigger_usd > 0
        )
        partial_enabled = partial_economic_ready
        target_source = str(decision.details.get("targetSource") or "")
        structural_liquidity_target = (
            target_source == "liquidity"
            or isinstance(decision.details.get("liquidityTarget"), dict)
        )
        runner_target_pct = target_pct
        if partial_enabled and not structural_liquidity_target:
            runner_target_pct = max(
                target_pct,
                stop_pct * max(
                    0.0,
                    self.config.runner_target_r,
                ),
            )
        runner_raw_price = (
            market_entry
            + direction
            * market_entry
            * runner_target_pct
        )

        if partial_enabled:
            runner_fraction = 1.0 - partial_fraction
            runner_quantity = quantity * runner_fraction
            runner_fill = apply_exit_slippage(
                runner_raw_price,
                side,
                target_exit_slippage_rate,
            )
            runner_raw_gross = (
                direction
                * (runner_raw_price - market_entry)
                * runner_quantity
            )
            runner_exit_fee_cost = (
                runner_quantity
                * runner_fill
                * target_exit_fee_rate
            )
            runner_slippage_cost = (
                runner_quantity
                * abs(runner_raw_price - runner_fill)
            )
            gross_profit = (
                partial_raw_gross
                + runner_raw_gross
            )
            lifecycle_fee_cost = (
                entry_fee_cost
                + partial_exit_fee_cost
                + runner_exit_fee_cost
            )
            lifecycle_slippage_cost = (
                partial_slippage_cost
                + runner_slippage_cost
            )
        else:
            runner_fraction = 1.0
            runner_quantity = quantity
            runner_raw_price = target
            runner_fill = full_target_fill
            gross_profit = (
                direction
                * (runner_raw_price - market_entry)
                * runner_quantity
            )
            runner_exit_fee_cost = (
                runner_quantity
                * runner_fill
                * target_exit_fee_rate
            )
            runner_slippage_cost = target_slippage_cost
            lifecycle_fee_cost = (
                entry_fee_cost
                + runner_exit_fee_cost
            )
            lifecycle_slippage_cost = (
                runner_slippage_cost
            )

        lifecycle_gross_pct = (
            gross_profit / notional
            if notional > 0
            else 0.0
        )
        lifecycle_fee_pct = (
            lifecycle_fee_cost / notional
            if notional > 0
            else 0.0
        )
        lifecycle_slippage_pct = (
            lifecycle_slippage_cost / notional
            if notional > 0
            else 0.0
        )
        lifecycle_cost_pct = (
            lifecycle_fee_pct
            + lifecycle_slippage_pct
        )
        first_take_move_pct = (
            partial_move_pct
            if partial_enabled
            else target_pct
        )
        minimum_first_take_move_failed = (
            first_take_move_pct
            < max(0.0, self.config.min_first_take_move_pct)
        )
        movement_floor_bands = {
            "0.10%": first_take_move_pct >= 0.0010,
            "0.15%": first_take_move_pct >= 0.0015,
            "0.20%": first_take_move_pct >= 0.0020,
            "0.25%": first_take_move_pct >= 0.0025,
            "0.30%": first_take_move_pct >= 0.0030,
        }
        estimated_costs = (
            lifecycle_fee_cost
            + lifecycle_slippage_cost
        )
        fee_cost = lifecycle_fee_cost
        slippage_cost = lifecycle_slippage_cost
        expected_net = gross_profit - estimated_costs
        winner_total_friction_usd = (
            estimated_costs
            + embedded_entry_slippage_usd
        )
        stop_total_friction_usd = (
            stop_estimated_costs
            + embedded_entry_slippage_usd
        )
        winner_cost_share = (
            winner_total_friction_usd
            / (
                gross_profit
                + embedded_entry_slippage_usd
            )
            if (
                gross_profit
                + embedded_entry_slippage_usd
                > 0
            )
            else float("inf")
        )
        stop_cost_share = (
            stop_total_friction_usd / gross_loss
            if gross_loss > 0
            else float("inf")
        )
        expected_net_loss = all_in_net_loss
        net_rr = (
            expected_net / all_in_net_loss
            if all_in_net_loss > 0
            else 0.0
        )

        minimum_net_reward = (
            all_in_net_loss * self.config.min_net_reward_risk
        )
        absolute_min_net_reward = (
            all_in_net_loss
            * max(
                0.0,
                self.config.absolute_min_net_reward_risk,
            )
        )
        minimum_net_profit_failed = expected_net < required_net_profit
        net_reward_risk_failed = expected_net < minimum_net_reward
        absolute_net_reward_risk_failed = (
            expected_net < absolute_min_net_reward
        )

        sizing_limits = {
            "structural_risk": notional_by_structural_risk,
            "trade_all_in_risk": notional_by_trade_all_in_cap,
            "portfolio_all_in_risk": notional_by_all_in_portfolio_risk,
            "portfolio_exposure": max(available_notional, 0),
            "position_exposure": position_exposure_cap,
            "visible_entry_depth": visible_entry_depth,
        }
        binding = min(sizing_limits, key=sizing_limits.get)
        if notional < min(sizing_limits.values()) - max(.01, notional*1e-5):
            binding = "depth_stress_or_quantity_rounding"
        economic_diagnostics = {
            "payoutMeaning": "conditional_on_targets_not_expected_value",
            "conditionalTargetNetUsd": expected_net,
            "executionBook": dict(depth.execution),
            "sizing": {"initialRiskNotionalUsd": initial_risk_notional,
                       "afterStrategyScaleNotionalUsd": notional_by_structural_risk,
                       "entryRiskFraction": entry_risk_fraction,
                       "limitsUsd": sizing_limits, "finalNotionalUsd": notional,
                       "bindingConstraint": binding, "strategyScale": risk_scale,
                       "exchangeLeverage": None,
                       "effectiveExposure": notional / balance if balance else 0.0,
                       "note": "paper notional/equity; no exchange leverage was set"},
            "riskBudgetUsd": structural_risk_budget,
            "baseStructuralRiskBudgetUsd": base_structural_risk_budget,
            "scaledStructuralRiskBudgetUsd": scaled_structural_risk_budget,
            "structuralRiskBudgetUsd": structural_risk_budget,
            "riskScale": risk_scale,
            "entryRiskFraction": entry_risk_fraction,
            "stagedEntryPhase": staged_entry.get("phase"),
            "existingPositionNotionalUsd": max(
                0.0,
                existing_position_notional,
            ),
            "existingPositionAllInRiskUsd": max(
                0.0,
                existing_position_all_in_risk_usd,
            ),
            "fullTradeAllInLossCapUsd": full_trade_all_in_cap_usd,
            "remainingTradeAllInLossCapUsd": trade_all_in_cap_usd,
            "fullPositionExposureCapUsd": full_position_exposure_cap,
            "remainingPositionExposureCapUsd": position_exposure_cap,
            "riskScaleSource": (
                (decision.details or {}).get("riskScaleSource")
            ),
            "tradeAllInLossCapUsd": trade_all_in_cap_usd,
            "riskSizingBasis": "structural_stop_with_all_in_cap",
            "notionalByRiskUsd": notional_by_structural_risk,
            "notionalByStructuralRiskUsd": notional_by_structural_risk,
            "notionalByTradeAllInCapUsd": notional_by_trade_all_in_cap,
            "notionalByAllInPortfolioRiskUsd": notional_by_all_in_portfolio_risk,
            "effectiveLeverage": notional / balance if balance else 0.0,
            "quantity": quantity,
            "instrument": instrument_diagnostics,
            "setupEntry": setup_entry,
            "marketEntry": market_entry,
            "rawExecutableEntry": float(raw_depth_entry),
            "expectedEntryFill": market_entry,
            "entrySlippageEmbeddedInFill": True,
            "embeddedEntrySlippageUsd": (
                embedded_entry_slippage_usd
            ),
            "stop": stop,
            "target": target,
            "stopDistancePct": stop_pct,
            "targetMovePct": target_pct,
            "targetCostPct": lifecycle_cost_pct,
            "fullTargetCostPct": target_cost_pct,
            "baseStopCostPct": stop_cost_pct,
            "stopDepthStressMultiplier": (
                self.config.stop_depth_stress_multiplier
            ),
            "stopDepthModel": stop_depth_model,
            "stopDepthReferencePrice": stop,
            "stopDepthIncludesPreTriggerLevels": False,
            "stopDepthImpactBps": (
                stop_depth_impact_rate * 10_000
            ),
            "stressedStopDepthImpactBps": (
                stop_depth_stress_rate * 10_000
            ),
            "stopDepthStressPct": stop_depth_stress_rate,
            "stopDepthStressCostUsd": stop_depth_stress_cost,
            "rawStopExitVwap": raw_stop_exit_vwap,
            "visibleStopDepthUsd": visible_stop_depth,
            "stopCostPct": stressed_stop_cost_pct,
            "winnerCostShare": winner_cost_share,
            "maximumWinnerCostShare": self.config.max_winner_cost_share,
            "winnerCostShareGateEnabled": self.config.enforce_winner_cost_share_gate,
            "stopCostShare": stop_cost_share,
            "maximumStopCostShare": self.config.max_stop_cost_share,
            "stopCostShareGateEnabled": self.config.enforce_stop_cost_share_gate,
            "partialPrice": partial_raw_price if partial_candidate else None,
            "partialCappedByImpulse": partial_capped,
            "impulseFirstTakeLimit": impulse_limit,
            "impulseFirstTakeLimitSource": impulse_limit_source,
            "firstTakePrice": partial_raw_price if partial_enabled else target,
            "partialCandidate": partial_candidate,
            "partialPlanned": partial_enabled,
            "configuredPartialFraction": partial_fraction,
            "partialFraction": (
                partial_fraction
                if partial_enabled
                else 0.0
            ),
            "partialMovePct": (
                partial_move_pct
                if partial_candidate
                else None
            ),
            "partialNetAtTriggerUsd": (
                partial_net_at_trigger_usd
            ),
            "partialRequiredNetUsd": (
                partial_required_net_usd
            ),
            "partialEconomicReady": (
                partial_economic_ready
            ),
            "firstTakeMovePct": first_take_move_pct,
            "minimumFirstTakeMovePct": self.config.min_first_take_move_pct,
            "firstTakeMoveGateEnabled": self.config.enforce_min_first_take_move_gate,
            "wouldFailFirstTakeMove": minimum_first_take_move_failed,
            "movementFloorBands": movement_floor_bands,
            "runnerFraction": runner_fraction if partial_enabled else 1.0,
            "runnerTargetPct": runner_target_pct,
            "lifecycleGrossPct": lifecycle_gross_pct,
            "lifecycleCostPct": lifecycle_cost_pct,
            "partialExitFeeRate": partial_exit_fee_rate,
            "partialExitSlippageRate": partial_exit_slippage_rate,
            "executionProfile": {
                **execution.public(),
                "entry": entry_mode,
                "baseEntry": execution.entry,
            },
            "entryMode": entry_mode,
            "feeSchedule": (
                fee_schedule.public()
                if fee_schedule is not None
                else None
            ),
            "entryFeeRate": entry_fee_rate,
            "targetExitFeeRate": target_exit_fee_rate,
            "stopExitFeeRate": stop_exit_fee_rate,
            "entrySlippageRate": entry_slippage_rate,
            "targetExitSlippageRate": target_exit_slippage_rate,
            "stopExitSlippageRate": stop_exit_slippage_rate,
            "allInLossPct": all_in_loss_pct,
            "targetEstimatedCostsUsd": estimated_costs,
            "stopEstimatedCostsUsd": stop_estimated_costs,
            "winnerTotalFrictionUsd": (
                winner_total_friction_usd
            ),
            "stopTotalFrictionUsd": (
                stop_total_friction_usd
            ),
            "entrySpreadPct": max(book.spread_pct, 0.0),
            "fastEntrySpreadPct": max(book.spread_pct, 0.0),
            "deepEntrySpreadPct": max(depth.spread_pct, 0.0),
            "fastBookBestBid": book.best_bid,
            "fastBookBestAsk": book.best_ask,
            "deepBookBestBid": depth.best_bid,
            "deepBookBestAsk": depth.best_ask,
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
            "absoluteMinimumNetRewardRisk": (
                self.config.absolute_min_net_reward_risk
            ),
            "wouldFailAbsoluteNetRewardRisk": (
                absolute_net_reward_risk_failed
            ),
            "minimumNetProfitGateEnabled": self.config.enforce_min_net_profit_gate,
            "payoffGateEnabled": self.config.enforce_net_reward_risk_gate,
            "requiredNetProfitUsd": required_net_profit,
            "requiredNetProfitEquityFraction": self.config.min_net_profit_equity_fraction,
            "wouldFailMinimumNetProfit": minimum_net_profit_failed,
            "wouldFailNetRewardRisk": net_reward_risk_failed,
        }

        if (
            partial_requested
            and impulse_limit is not None
            and not partial_economic_ready
            and direction * (target - impulse_limit) > 1e-12
        ):
            return RiskResult(
                False,
                "economic_safety: impulse first take cannot cover planned costs/profit floor",
                diagnostics=dict(economic_diagnostics),
            )

        if (
            self.config.enforce_min_first_take_move_gate
            and minimum_first_take_move_failed
        ):
            return RiskResult(
                False,
                (
                    "movement_gate: first_take_move "
                    f"{first_take_move_pct * 100:.3f}% < minimum "
                    f"{self.config.min_first_take_move_pct * 100:.3f}%"
                ),
                diagnostics=dict(economic_diagnostics),
            )

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
        if absolute_net_reward_risk_failed:
            return RiskResult(
                False,
                (
                    "economic_safety: absolute_net_reward_risk: "
                    f"net reward/risk {net_rr:.4f} < hard minimum "
                    f"{self.config.absolute_min_net_reward_risk:.4f}"
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
            "roundTripCostPct": stressed_stop_cost_pct,
            "lifecycleCostPct": lifecycle_cost_pct,
            "firstTakeMovePct": first_take_move_pct,
            "minimumFirstTakeMovePct": self.config.min_first_take_move_pct,
            "movementFloorBands": movement_floor_bands,
            "winnerCostShare": winner_cost_share,
            "stopCostShare": stop_cost_share,
            "takerFeeCostUsd": fee_cost,
            "slippageCostUsd": slippage_cost,
            "estimatedCostsUsd": estimated_costs,
            "spreadCostDoubleCounted": False,
            "netRewardRiskRatio": net_rr,
            "minimumNetRewardRiskRatio": self.config.min_net_reward_risk,
            "absoluteMinimumNetRewardRiskRatio": (
                self.config.absolute_min_net_reward_risk
            ),
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
        if fee_schedule is not None:
            strategy_details["feeSchedule"] = (
                fee_schedule.public()
            )
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
            entry_mode=entry_mode,
            quantity=quantity,
            strategy_details=strategy_details,
        )
        return RiskResult(
            True,
            "allowed",
            plan,
            diagnostics=dict(economics),
        )
