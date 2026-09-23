from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import time
from typing import Any

from .config import Settings
from .domain import OrderBook, Side, TradePlan
from .execution import execution_profile, fee_rate, slippage_rate
from .strategy_policy import no_follow_through_seconds, partial_take_fraction


@dataclass(slots=True)
class Position:
    symbol: str
    strategy: str
    side: Side
    original_notional: float
    notional: float
    setup_entry: float
    entry: float
    initial_stop: float
    stop: float
    target: float
    opened_at: float
    entry_fee_remaining: float
    entry_fee_total_usd: float
    last_price: float
    setup_id: str
    strategy_details: dict[str, Any] = field(default_factory=dict)
    partial_taken: bool = False
    realized_gross_usd: float = 0.0
    realized_net_usd: float = 0.0
    fees_paid_usd: float = 0.0
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    unrealized_pnl: float = 0.0
    partial_net_preview_usd: float = 0.0
    partial_required_net_usd: float = 0.0
    partial_economic_ready: bool = False
    current_move_pct: float = 0.0
    max_favorable_move_pct: float = 0.0
    max_adverse_move_pct: float = 0.0
    mfe_price: float | None = None
    mae_price: float | None = None
    mfe_at: float | None = None
    mae_at: float | None = None
    partial_taken_at: float | None = None
    estimated_exit_fee_usd: float = 0.0
    initial_risk_budget_usd: float = 0.0
    entry_legs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def initial_risk_usd(self) -> float:
        if self.initial_risk_budget_usd > 0:
            return self.initial_risk_budget_usd
        return (
            self.original_notional
            * abs(self.entry - self.initial_stop)
            / self.entry
            if self.entry > 0
            else 0.0
        )

    @property
    def current_risk_usd(self) -> float:
        if self.side == Side.LONG:
            adverse = max(0.0, self.entry - self.stop)
        else:
            adverse = max(0.0, self.stop - self.entry)
        return self.notional * adverse / self.entry

    def cost_reserve_usd(self, config: Settings) -> float:
        profile = execution_profile(self.strategy)
        exit_fee = self.notional * fee_rate(
            config,
            profile.stop_exit,
        )
        exit_slippage = self.notional * slippage_rate(
            config,
            profile.stop_exit,
        )
        return (
            max(0.0, self.entry_fee_remaining)
            + exit_fee
            + exit_slippage
        )

    def all_in_risk_usd(self, config: Settings) -> float:
        return self.current_risk_usd + self.cost_reserve_usd(config)

    @property
    def mfe_r(self) -> float:
        return self.mfe_usd / self.initial_risk_usd if self.initial_risk_usd > 0 else 0.0

    @property
    def mae_r(self) -> float:
        return self.mae_usd / self.initial_risk_usd if self.initial_risk_usd > 0 else 0.0

    def public(self) -> dict:
        data = asdict(self)
        data["side"] = self.side.value
        data["initial_risk_usd"] = self.initial_risk_usd
        data["current_risk_usd"] = self.current_risk_usd
        data["mfe_r"] = self.mfe_r
        data["mae_r"] = self.mae_r
        data["fees_committed_usd"] = (
            self.fees_paid_usd + self.entry_fee_remaining
        )
        data["estimated_total_fees_if_close_now_usd"] = (
            self.fees_paid_usd
            + self.entry_fee_remaining
            + self.estimated_exit_fee_usd
        )
        economics = (
            self.strategy_details.get("economics")
            if isinstance(self.strategy_details, dict)
            else None
        )
        if isinstance(economics, dict):
            data["planned_first_take_move_pct"] = economics.get(
                "firstTakeMovePct"
            )
            data["planned_target_move_pct"] = economics.get(
                "targetMovePct"
            )
            data["movement_floor_bands"] = economics.get(
                "movementFloorBands"
            )
        return data


@dataclass(slots=True)
class PendingEntry:
    plan: TradePlan
    limit_price: float
    created_at: float
    expires_at: float
    min_trade_ts_ms: int | None = None
    position_action: str = "open"

    def public(self) -> dict:
        return {
            "symbol": self.plan.symbol,
            "strategy": self.plan.strategy,
            "side": self.plan.side.value,
            "setupId": self.plan.setup_id,
            "limitPrice": self.limit_price,
            "notional": self.plan.notional,
            "expectedNetLoss": self.plan.expected_net_loss,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "minTradeTsMs": self.min_trade_ts_ms,
            "entryMode": self.plan.entry_mode,
            "positionAction": self.position_action,
        }


class PaperBroker:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.balance = config.start_balance
        self.start_balance = config.start_balance
        self.positions: dict[str, Position] = {}
        self.pending_entries: dict[str, PendingEntry] = {}
        self.closed_trades: list[dict] = []
        self.total_closed_trades: int = 0

    @property
    def total_pnl(self) -> float:
        return self.balance - self.start_balance

    @property
    def total_exposure(self) -> float:
        return sum(x.notional for x in self.positions.values())

    @property
    def open_structural_risk_usd(self) -> float:
        return sum(
            x.current_risk_usd
            for x in self.positions.values()
        )

    @property
    def open_cost_reserve_usd(self) -> float:
        return sum(
            x.cost_reserve_usd(self.config)
            for x in self.positions.values()
        )

    @property
    def open_risk_usd(self) -> float:
        return sum(
            x.all_in_risk_usd(self.config)
            for x in self.positions.values()
        )

    @property
    def pending_exposure_usd(self) -> float:
        return sum(x.plan.notional for x in self.pending_entries.values())

    @property
    def pending_risk_usd(self) -> float:
        return sum(x.plan.expected_net_loss for x in self.pending_entries.values())

    @property
    def available_notional(self) -> float:
        cap = self.balance * self.config.max_leverage
        return max(0.0, cap - self.total_exposure - self.pending_exposure_usd)

    @property
    def available_risk_usd(self) -> float:
        cap = self.balance * self.config.max_total_risk_fraction
        return max(0.0, cap - self.open_risk_usd - self.pending_risk_usd)

    def can_open(self, symbol: str) -> tuple[bool, str]:
        if symbol in self.positions:
            return False, "symbol already has an open position"
        if symbol in self.pending_entries:
            return False, "symbol already has a pending entry"
        if len(self.positions) + len(self.pending_entries) >= self.config.max_open_positions:
            return False, "maximum open positions reached"
        if (
            self.config.enforce_session_loss_limit
            and self.config.max_daily_loss_fraction > 0
            and self.total_pnl <= -(self.start_balance * self.config.max_daily_loss_fraction)
        ):
            return False, "session loss limit reached"
        if self.available_notional <= 0:
            return False, "portfolio exposure budget exhausted"
        if self.available_risk_usd <= 0:
            return False, "portfolio risk budget exhausted"
        return True, "allowed"

    def can_add(self, plan: TradePlan) -> tuple[bool, str]:
        pos = self.positions.get(plan.symbol)
        if pos is None:
            return False, "no open position for staged add"
        if plan.symbol in self.pending_entries:
            return False, "symbol already has a pending entry"
        if pos.strategy != plan.strategy:
            return False, "staged add strategy does not match open position"
        if pos.side != plan.side:
            return False, "staged add side does not match open position"
        if pos.setup_id != plan.setup_id:
            return False, "staged add setup does not match open position"
        if pos.partial_taken:
            return False, "cannot add after partial take"
        if (
            self.config.enforce_session_loss_limit
            and self.config.max_daily_loss_fraction > 0
            and self.total_pnl
            <= -(
                self.start_balance
                * self.config.max_daily_loss_fraction
            )
        ):
            return False, "session loss limit reached"
        if self.available_notional <= 0:
            return False, "portfolio exposure budget exhausted"
        if self.available_risk_usd <= 0:
            return False, "portfolio risk budget exhausted"
        return True, "allowed"

    def _position_from_fill(
        self,
        plan: TradePlan,
        fill: float,
        entry_fee: float,
    ) -> Position:
        structural_risk_usd = (
            plan.notional * abs(fill - plan.stop) / fill
            if fill > 0
            else 0.0
        )
        position = Position(
            symbol=plan.symbol,
            strategy=plan.strategy,
            side=plan.side,
            original_notional=plan.notional,
            notional=plan.notional,
            setup_entry=plan.setup_entry,
            entry=fill,
            initial_stop=plan.stop,
            stop=plan.stop,
            target=plan.target,
            opened_at=time(),
            entry_fee_remaining=entry_fee,
            entry_fee_total_usd=entry_fee,
            last_price=fill,
            setup_id=plan.setup_id,
            strategy_details=dict(plan.strategy_details),
            initial_risk_budget_usd=structural_risk_usd,
            entry_legs=[{
                "phase": str(
                    (
                        plan.strategy_details.get("stagedEntry")
                        or {}
                    ).get("phase") or "full"
                ),
                "notional": plan.notional,
                "fill": fill,
                "entryFeeUsd": entry_fee,
                "structuralRiskUsd": structural_risk_usd,
                "addedAt": time(),
                "plan": plan.public(),
            }],
        )
        self.positions[plan.symbol] = position
        return position

    def _add_to_position_from_fill(
        self,
        plan: TradePlan,
        fill: float,
        entry_fee: float,
    ) -> Position:
        pos = self.positions.get(plan.symbol)
        if pos is None:
            raise RuntimeError("no open position for staged add")
        previous_notional = pos.notional
        combined_notional = previous_notional + plan.notional
        if combined_notional <= 0:
            raise RuntimeError("invalid combined staged position size")

        weighted_entry = (
            pos.entry * previous_notional
            + fill * plan.notional
        ) / combined_notional
        incremental_structural_risk = (
            plan.notional * abs(fill - plan.stop) / fill
            if fill > 0
            else 0.0
        )

        pos.original_notional += plan.notional
        pos.notional = combined_notional
        pos.entry = weighted_entry
        pos.entry_fee_remaining += entry_fee
        pos.entry_fee_total_usd += entry_fee
        pos.last_price = fill
        pos.initial_risk_budget_usd += (
            incremental_structural_risk
        )

        # A confirmation leg may tighten invalidation, but never widens the
        # risk of the already-open probe.
        if pos.side == Side.LONG:
            pos.stop = max(pos.stop, plan.stop)
        else:
            pos.stop = min(pos.stop, plan.stop)

        merged_details = dict(pos.strategy_details)
        merged_details.update(plan.strategy_details)
        pos.strategy_details = merged_details
        pos.entry_legs.append({
            "phase": str(
                (
                    plan.strategy_details.get("stagedEntry")
                    or {}
                ).get("phase") or "add"
            ),
            "notional": plan.notional,
            "fill": fill,
            "entryFeeUsd": entry_fee,
            "structuralRiskUsd": incremental_structural_risk,
            "addedAt": time(),
            "plan": plan.public(),
        })
        return pos

    def place_pending(
        self,
        plan: TradePlan,
        *,
        min_trade_ts_ms: int | None = None,
    ) -> PendingEntry:
        if plan.entry_mode != "maker_limit":
            raise RuntimeError("pending entry requires maker_limit plan")
        allowed, reason = self.can_open(plan.symbol)
        if not allowed:
            raise RuntimeError(reason)
        if plan.notional > self.available_notional + 1e-9:
            raise RuntimeError("plan exceeds remaining portfolio exposure budget")
        if plan.expected_net_loss > self.available_risk_usd + 1e-9:
            raise RuntimeError("plan exceeds remaining all-in portfolio risk budget")
        now = time()
        pending = PendingEntry(
            plan=plan,
            limit_price=plan.market_entry,
            created_at=now,
            expires_at=now + max(0.1, self.config.passive_entry_timeout_seconds),
            min_trade_ts_ms=min_trade_ts_ms,
        )
        self.pending_entries[plan.symbol] = pending
        return pending

    def place_pending_add(
        self,
        plan: TradePlan,
        *,
        min_trade_ts_ms: int | None = None,
    ) -> PendingEntry:
        if plan.entry_mode != "maker_limit":
            raise RuntimeError(
                "pending staged add requires maker_limit plan"
            )
        allowed, reason = self.can_add(plan)
        if not allowed:
            raise RuntimeError(reason)
        if plan.notional > self.available_notional + 1e-9:
            raise RuntimeError(
                "staged add exceeds remaining portfolio exposure budget"
            )
        if plan.expected_net_loss > self.available_risk_usd + 1e-9:
            raise RuntimeError(
                "staged add exceeds remaining all-in portfolio risk budget"
            )
        now = time()
        pending = PendingEntry(
            plan=plan,
            limit_price=plan.market_entry,
            created_at=now,
            expires_at=now
            + max(0.1, self.config.passive_entry_timeout_seconds),
            min_trade_ts_ms=min_trade_ts_ms,
            position_action="add",
        )
        self.pending_entries[plan.symbol] = pending
        return pending

    def expire_pending(
        self,
        now: float | None = None,
    ) -> list[dict]:
        resolved = time() if now is None else now
        events: list[dict] = []
        for symbol, pending in list(self.pending_entries.items()):
            if resolved < pending.expires_at:
                continue
            del self.pending_entries[symbol]
            events.append({
                "event": "entry_cancelled",
                "symbol": symbol,
                "strategy": pending.plan.strategy,
                "setupId": pending.plan.setup_id,
                "reason": "passive_entry_timeout",
                "limitPrice": pending.limit_price,
            })
        return events

    def mark_pending(
        self,
        symbol: str,
        last_trade_price: float,
        *,
        trade_ts_ms: int | None = None,
    ) -> list[dict]:
        pending = self.pending_entries.get(symbol)
        if pending is None:
            return []
        now = time()
        if now >= pending.expires_at:
            del self.pending_entries[symbol]
            return [{
                "event": "entry_cancelled",
                "symbol": symbol,
                "strategy": pending.plan.strategy,
                "setupId": pending.plan.setup_id,
                "reason": "passive_entry_timeout",
                "limitPrice": pending.limit_price,
            }]
        if (
            pending.min_trade_ts_ms is not None
            and trade_ts_ms is not None
            and trade_ts_ms <= pending.min_trade_ts_ms
        ):
            return []
        confirm = max(0.0, self.config.maker_fill_confirmation_bps) / 10_000
        if pending.plan.side == Side.LONG:
            filled = last_trade_price <= pending.limit_price * (1 - confirm)
        else:
            filled = last_trade_price >= pending.limit_price * (1 + confirm)
        if not filled:
            return []
        del self.pending_entries[symbol]
        is_add = pending.position_action == "add"
        if is_add:
            allowed, reason = self.can_add(pending.plan)
        else:
            allowed, reason = self.can_open(symbol)
        if not allowed:
            return [{
                "event": "entry_cancelled",
                "symbol": symbol,
                "strategy": pending.plan.strategy,
                "setupId": pending.plan.setup_id,
                "reason": f"passive_fill_blocked: {reason}",
                "limitPrice": pending.limit_price,
                "positionAction": pending.position_action,
            }]
        if pending.plan.notional > self.available_notional + 1e-9:
            return [{
                "event": "entry_cancelled",
                "symbol": symbol,
                "strategy": pending.plan.strategy,
                "setupId": pending.plan.setup_id,
                "reason": "passive_fill_exposure_budget",
                "limitPrice": pending.limit_price,
                "positionAction": pending.position_action,
            }]
        if pending.plan.expected_net_loss > self.available_risk_usd + 1e-9:
            return [{
                "event": "entry_cancelled",
                "symbol": symbol,
                "strategy": pending.plan.strategy,
                "setupId": pending.plan.setup_id,
                "reason": "passive_fill_risk_budget",
                "limitPrice": pending.limit_price,
                "positionAction": pending.position_action,
            }]
        fee = pending.plan.notional * fee_rate(
            self.config,
            "maker_limit",
        )
        if is_add:
            position = self._add_to_position_from_fill(
                pending.plan,
                pending.limit_price,
                fee,
            )
            event_type = "entry_added"
        else:
            position = self._position_from_fill(
                pending.plan,
                pending.limit_price,
                fee,
            )
            event_type = "entry_filled"
        return [{
            "event": event_type,
            "symbol": symbol,
            "strategy": pending.plan.strategy,
            "setupId": pending.plan.setup_id,
            "plan": pending.plan.public(),
            "position": position.public(),
            "limitPrice": pending.limit_price,
            "fillModel": "trade_through",
            "positionAction": pending.position_action,
        }]

    def cancel_all_pending(self, reason: str) -> list[dict]:
        events = [{
            "event": "entry_cancelled",
            "symbol": symbol,
            "strategy": pending.plan.strategy,
            "setupId": pending.plan.setup_id,
            "reason": reason,
            "limitPrice": pending.limit_price,
        } for symbol, pending in self.pending_entries.items()]
        self.pending_entries.clear()
        return events

    def open(self, plan: TradePlan, book: OrderBook) -> Position:
        if plan.entry_mode == "maker_limit":
            raise RuntimeError("maker_limit plan must be placed as pending entry")
        allowed, reason = self.can_open(plan.symbol)
        if not allowed:
            raise RuntimeError(reason)
        if plan.notional > self.available_notional + 1e-9:
            raise RuntimeError(
                "plan exceeds remaining portfolio exposure budget"
            )
        if plan.expected_net_loss > self.available_risk_usd + 1e-9:
            raise RuntimeError(
                "plan exceeds remaining all-in portfolio risk budget"
            )
        raw, visible_depth = book.entry_vwap(
            plan.side,
            plan.notional,
        )
        if (
            raw is None
            or visible_depth + max(1e-9, plan.notional * 1e-9)
            < plan.notional
        ):
            raise RuntimeError("insufficient visible entry depth")
        profile = execution_profile(plan.strategy)
        slip = slippage_rate(self.config, profile.entry)
        fill = raw * (1 + slip if plan.side == Side.LONG else 1 - slip)
        fee = plan.notional * fee_rate(
            self.config,
            profile.entry,
        )
        return self._position_from_fill(plan, fill, fee)

    def add(self, plan: TradePlan, book: OrderBook) -> Position:
        if plan.entry_mode == "maker_limit":
            raise RuntimeError(
                "maker_limit staged add must be placed as pending entry"
            )
        allowed, reason = self.can_add(plan)
        if not allowed:
            raise RuntimeError(reason)
        if plan.notional > self.available_notional + 1e-9:
            raise RuntimeError(
                "staged add exceeds remaining portfolio exposure budget"
            )
        if plan.expected_net_loss > self.available_risk_usd + 1e-9:
            raise RuntimeError(
                "staged add exceeds remaining all-in portfolio risk budget"
            )
        raw, visible_depth = book.entry_vwap(
            plan.side,
            plan.notional,
        )
        if (
            raw is None
            or visible_depth
            + max(1e-9, plan.notional * 1e-9)
            < plan.notional
        ):
            raise RuntimeError(
                "insufficient visible entry depth for staged add"
            )
        profile = execution_profile(plan.strategy)
        slip = slippage_rate(self.config, profile.entry)
        fill = raw * (
            1 + slip
            if plan.side == Side.LONG
            else 1 - slip
        )
        fee = plan.notional * fee_rate(
            self.config,
            profile.entry,
        )
        return self._add_to_position_from_fill(
            plan,
            fill,
            fee,
        )

    def mark(self, symbol: str, last_price: float, book: OrderBook) -> list[dict]:
        pos = self.positions.get(symbol)
        if pos is None:
            return []

        pos.last_price = last_price
        direction = 1 if pos.side == Side.LONG else -1
        executable = book.executable_exit(pos.side) or last_price
        now = time()
        directional_move_pct = (
            direction * (executable - pos.entry) / pos.entry
            if pos.entry > 0
            else 0.0
        )
        pos.current_move_pct = directional_move_pct
        if directional_move_pct > pos.max_favorable_move_pct:
            pos.max_favorable_move_pct = directional_move_pct
            pos.mfe_price = executable
            pos.mfe_at = now
        adverse_move_pct = max(0.0, -directional_move_pct)
        if adverse_move_pct > pos.max_adverse_move_pct:
            pos.max_adverse_move_pct = adverse_move_pct
            pos.mae_price = executable
            pos.mae_at = now
        profile = execution_profile(pos.strategy)
        pos.estimated_exit_fee_usd = (
            pos.notional * fee_rate(
                self.config,
                profile.stop_exit,
            )
        )

        # All lifecycle triggers use an executable exit price, not the public
        # last trade. This prevents a target from firing when the bid/ask plus
        # spread is still below the actual executable threshold.
        gross_mark_original = (
            direction * (executable - pos.entry) / pos.entry * pos.original_notional
        )
        pos.mfe_usd = max(pos.mfe_usd, gross_mark_original)
        pos.mae_usd = max(pos.mae_usd, -gross_mark_original)

        pos.unrealized_pnl = direction * (executable - pos.entry) / pos.entry * pos.notional
        pos.unrealized_pnl -= pos.entry_fee_remaining + pos.notional * self.config.taker_fee_rate

        hit_stop = executable <= pos.stop if pos.side == Side.LONG else executable >= pos.stop
        if hit_stop:
            return [self.close(symbol, book, "stop")]

        events: list[dict] = []
        allow_runner = bool(pos.strategy_details.get("allowRunner", True))
        partial_triggered = self._partial_triggered(
            pos,
            executable,
            profile.partial_exit,
        )
        if (
            self.config.partial_take_enabled
            and allow_runner
            and not pos.partial_taken
            and partial_triggered
        ):
            close_notional = self._partial_close_notional(pos)
            preview = self._preview_realize(
                pos,
                close_notional,
                book,
                reason="partial_take",
            )
            required_net = self._partial_required_net_usd(pos)
            pos.partial_net_preview_usd = preview["net"]
            pos.partial_required_net_usd = required_net
            pos.partial_economic_ready = preview["net"] >= required_net
            if pos.partial_economic_ready:
                events.append(
                    self._partial_take(
                        pos,
                        book,
                        preview=preview,
                    )
                )

        if symbol not in self.positions:
            return events

        pos = self.positions[symbol]
        hit_target = executable >= pos.target if pos.side == Side.LONG else executable <= pos.target
        if hit_target:
            events.append(self.close(symbol, book, "runner_target" if pos.partial_taken else "target"))
            return events

        if not pos.partial_taken and self._should_cut_no_follow_through(pos, gross_mark_original):
            events.append(self.close(symbol, book, "no_follow_through"))
        return events

    def close(self, symbol: str, book: OrderBook, reason: str) -> dict:
        pos = self.positions.get(symbol)
        if pos is None:
            raise RuntimeError("no paper position for symbol")
        final_leg = self._realize(
            pos,
            pos.notional,
            book,
            reason=reason,
        )
        direction = 1 if pos.side == Side.LONG else -1
        exit_move_pct = (
            direction * (final_leg["fill"] - pos.entry) / pos.entry
            if pos.entry > 0
            else 0.0
        )
        economics = (
            pos.strategy_details.get("economics")
            if isinstance(pos.strategy_details, dict)
            else None
        )
        trade = {
            "event": "trade_closed",
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "side": pos.side.value,
            "setupId": pos.setup_id,
            "setupEntry": pos.setup_entry,
            "entry": pos.entry,
            "exit": final_leg["fill"],
            "exitMovePct": exit_move_pct,
            "exitMoveBps": exit_move_pct * 10_000,
            "currentMovePct": pos.current_move_pct,
            "maxFavorableMovePct": pos.max_favorable_move_pct,
            "maxFavorableMoveBps": pos.max_favorable_move_pct * 10_000,
            "maxAdverseMovePct": pos.max_adverse_move_pct,
            "maxAdverseMoveBps": pos.max_adverse_move_pct * 10_000,
            "mfePrice": pos.mfe_price,
            "maePrice": pos.mae_price,
            "mfeAt": pos.mfe_at,
            "maeAt": pos.mae_at,
            "initialStop": pos.initial_stop,
            "stop": pos.stop,
            "target": pos.target,
            "originalNotional": pos.original_notional,
            "entryLegs": list(pos.entry_legs),
            "scaleInCount": max(0, len(pos.entry_legs) - 1),
            "grossPnl": pos.realized_gross_usd,
            "fees": pos.fees_paid_usd,
            "entryFeeUsd": pos.entry_fee_total_usd,
            "plannedFirstTakeMovePct": (
                economics.get("firstTakeMovePct")
                if isinstance(economics, dict)
                else None
            ),
            "movementFloorBands": (
                economics.get("movementFloorBands")
                if isinstance(economics, dict)
                else None
            ),
            "netPnl": pos.realized_net_usd,
            "partialTaken": pos.partial_taken,
            "mfeUsd": pos.mfe_usd,
            "maeUsd": pos.mae_usd,
            "mfeR": pos.mfe_r,
            "maeR": pos.mae_r,
            "initialRiskUsd": pos.initial_risk_usd,
            "reason": reason,
            "openedAt": pos.opened_at,
            "partialTakenAt": pos.partial_taken_at,
            "closedAt": time(),
            "strategyDetails": dict(pos.strategy_details),
        }
        self.total_closed_trades += 1
        self.closed_trades.append(trade)
        self.closed_trades = self.closed_trades[-200:]
        del self.positions[symbol]
        return trade

    @staticmethod
    def _initial_risk_distance(pos: Position) -> float:
        if pos.original_notional <= 0 or pos.entry <= 0:
            return abs(pos.entry - pos.initial_stop)
        return (
            pos.initial_risk_usd
            / pos.original_notional
            * pos.entry
        )

    def _partial_limit_price(self, pos: Position) -> float:
        risk_distance = self._initial_risk_distance(pos)
        multiple = max(0.0, self.config.partial_take_at_r)
        if pos.side == Side.LONG:
            return pos.entry + risk_distance * multiple
        return pos.entry - risk_distance * multiple

    def _partial_triggered(
        self,
        pos: Position,
        executable: float,
        exit_mode: str,
    ) -> bool:
        if pos.initial_risk_usd <= 0:
            return False
        if exit_mode != "maker_limit":
            return pos.mfe_r >= self.config.partial_take_at_r
        limit_price = self._partial_limit_price(pos)
        confirm = max(0.0, self.config.maker_fill_confirmation_bps) / 10_000
        if pos.side == Side.LONG:
            return executable >= limit_price * (1 + confirm)
        return executable <= limit_price * (1 - confirm)

    def _partial_close_notional(self, pos: Position) -> float:
        return min(
            pos.notional,
            pos.original_notional
            * partial_take_fraction(
                self.config,
                pos.strategy,
            ),
        )

    @staticmethod
    def _partial_required_net_usd(pos: Position) -> float:
        economics = (
            pos.strategy_details.get("economics")
            if isinstance(pos.strategy_details, dict)
            else None
        )
        if not isinstance(economics, dict):
            return 0.0
        if not bool(
            economics.get("minimumNetProfitGateEnabled", True)
        ):
            return 0.0
        return max(
            0.0,
            float(economics.get("requiredNetProfitUsd") or 0.0),
        )

    def _runner_breakeven_stop(self, pos: Position) -> float:
        if pos.notional <= 0:
            return pos.entry
        profile = execution_profile(pos.strategy)
        exit_fee = pos.notional * fee_rate(
            self.config,
            profile.stop_exit,
        )
        profit_buffer = (
            pos.notional
            * self.config.breakeven_buffer_bps
            / 10_000
        )
        gross_needed = (
            max(0.0, pos.entry_fee_remaining)
            + exit_fee
            + profit_buffer
        )
        gross_needed_pct = gross_needed / pos.notional
        slip = slippage_rate(
            self.config,
            profile.stop_exit,
        )

        if pos.side == Side.LONG:
            required_fill = pos.entry * (1 + gross_needed_pct)
            return required_fill / max(1e-9, 1 - slip)

        required_fill = pos.entry * (1 - gross_needed_pct)
        return required_fill / (1 + slip)

    def _partial_take(
        self,
        pos: Position,
        book: OrderBook,
        *,
        preview: dict | None = None,
    ) -> dict:
        close_notional = self._partial_close_notional(pos)
        required_net = self._partial_required_net_usd(pos)
        preview = preview or self._preview_realize(
            pos,
            close_notional,
            book,
            reason="partial_take",
        )
        if preview["net"] < required_net:
            raise RuntimeError(
                "partial take attempted before economic threshold"
            )

        leg = self._realize(
            pos,
            close_notional,
            book,
            reason="partial_take",
        )
        pos.partial_taken = True
        pos.partial_taken_at = time()
        pos.partial_net_preview_usd = leg["net"]
        pos.partial_required_net_usd = required_net
        pos.partial_economic_ready = True

        risk_distance = self._initial_risk_distance(pos)
        runner_stop = self._runner_breakeven_stop(pos)
        target_source = str(
            pos.strategy_details.get("targetSource") or ""
        )
        structural_liquidity_target = (
            target_source == "liquidity"
            or isinstance(
                pos.strategy_details.get("liquidityTarget"),
                dict,
            )
        )
        if pos.side == Side.LONG:
            pos.stop = max(pos.stop, runner_stop)
            if not structural_liquidity_target:
                pos.target = max(
                    pos.target,
                    pos.entry
                    + risk_distance * self.config.runner_target_r,
                )
        else:
            pos.stop = min(pos.stop, runner_stop)
            if not structural_liquidity_target:
                pos.target = min(
                    pos.target,
                    pos.entry
                    - risk_distance * self.config.runner_target_r,
                )

        return {
            "event": "partial_take",
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "side": pos.side.value,
            "setupId": pos.setup_id,
            "fill": leg["fill"],
            "movePct": (
                (1 if pos.side == Side.LONG else -1)
                * (leg["fill"] - pos.entry)
                / pos.entry
                if pos.entry > 0
                else 0.0
            ),
            "moveBps": (
                (1 if pos.side == Side.LONG else -1)
                * (leg["fill"] - pos.entry)
                / pos.entry
                * 10_000
                if pos.entry > 0
                else 0.0
            ),
            "takenAt": pos.partial_taken_at,
            "closedNotional": close_notional,
            "remainingNotional": pos.notional,
            "grossPnl": leg["gross"],
            "fees": leg["fees"],
            "netPnl": leg["net"],
            "requiredNetUsd": required_net,
            "economicReady": True,
            "realizedNetTotal": pos.realized_net_usd,
            "newStop": pos.stop,
            "newTarget": pos.target,
            "mfeR": pos.mfe_r,
            "reason": "partial_take_at_r_and_net",
        }

    def _preview_realize(
        self,
        pos: Position,
        close_notional: float,
        book: OrderBook,
        *,
        reason: str = "market_exit",
    ) -> dict:
        if close_notional <= 0 or pos.notional <= 0:
            return {
                "fill": pos.last_price,
                "gross": 0.0,
                "fees": 0.0,
                "net": 0.0,
            }

        close_notional = min(close_notional, pos.notional)
        profile = execution_profile(pos.strategy)
        target_limit = (
            reason in {"target", "runner_target"}
            and profile.target_exit == "maker_limit"
        )
        partial_limit = (
            reason == "partial_take"
            and profile.partial_exit == "maker_limit"
        )
        if target_limit:
            raw = pos.target
            exit_mode = profile.target_exit
        elif partial_limit:
            raw = self._partial_limit_price(pos)
            exit_mode = profile.partial_exit
        else:
            raw, visible_depth = book.exit_vwap(
                pos.side,
                close_notional,
            )
            if raw is None:
                raw = book.executable_exit(pos.side) or pos.last_price
            elif (
                visible_depth + max(1e-9, close_notional * 1e-9)
                < close_notional
            ):
                levels = (
                    book.bids
                    if pos.side == Side.LONG
                    else book.asks
                )
                if levels:
                    worst = levels[-1][0]
                    visible_base = (
                        visible_depth / raw
                        if raw > 0
                        else 0.0
                    )
                    missing = max(
                        0.0,
                        close_notional - visible_depth,
                    )
                    total_base = visible_base + (
                        missing / worst
                        if worst > 0
                        else 0.0
                    )
                    if total_base > 0:
                        raw = close_notional / total_base
            exit_mode = (
                profile.partial_exit
                if reason == "partial_take"
                else profile.stop_exit
            )
        slip = slippage_rate(
            self.config,
            exit_mode,
        )
        fill = raw * (
            1 - slip
            if pos.side == Side.LONG
            else 1 + slip
        )
        direction = 1 if pos.side == Side.LONG else -1
        gross = (
            direction
            * (fill - pos.entry)
            / pos.entry
            * close_notional
        )
        share = close_notional / pos.notional
        allocated_entry_fee = pos.entry_fee_remaining * share
        exit_fee = close_notional * fee_rate(
            self.config,
            exit_mode,
        )
        fees = allocated_entry_fee + exit_fee
        return {
            "fill": fill,
            "gross": gross,
            "fees": fees,
            "net": gross - fees,
        }

    def _realize(
        self,
        pos: Position,
        close_notional: float,
        book: OrderBook,
        *,
        reason: str = "market_exit",
    ) -> dict:
        if close_notional <= 0 or pos.notional <= 0:
            return {"fill": pos.last_price, "gross": 0.0, "fees": 0.0, "net": 0.0}

        close_notional = min(close_notional, pos.notional)
        leg = self._preview_realize(
            pos,
            close_notional,
            book,
            reason=reason,
        )
        fill = leg["fill"]
        gross = leg["gross"]
        fees = leg["fees"]
        net = leg["net"]
        share = close_notional / pos.notional
        allocated_entry_fee = pos.entry_fee_remaining * share

        pos.notional -= close_notional
        pos.entry_fee_remaining -= allocated_entry_fee
        pos.realized_gross_usd += gross
        pos.realized_net_usd += net
        pos.fees_paid_usd += fees
        self.balance += net

        return {"fill": fill, "gross": gross, "fees": fees, "net": net}

    def _should_cut_no_follow_through(self, pos: Position, gross_mark_original: float) -> bool:
        age = time() - pos.opened_at
        timeout = no_follow_through_seconds(
            self.config,
            pos.strategy,
        )
        if age < timeout or pos.initial_risk_usd <= 0:
            return False
        adverse_r = max(0.0, -gross_mark_original) / pos.initial_risk_usd
        return (
            pos.mfe_r < self.config.no_follow_through_max_mfe_r
            and adverse_r >= self.config.early_cut_at_r
        )
