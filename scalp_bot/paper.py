from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import time
from typing import Any

from .config import Settings
from .domain import OrderBook, Side, TradePlan


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

    @property
    def initial_risk_usd(self) -> float:
        return self.original_notional * abs(self.entry - self.initial_stop) / self.entry

    @property
    def current_risk_usd(self) -> float:
        if self.side == Side.LONG:
            adverse = max(0.0, self.entry - self.stop)
        else:
            adverse = max(0.0, self.stop - self.entry)
        return self.notional * adverse / self.entry

    def cost_reserve_usd(self, config: Settings) -> float:
        exit_fee = self.notional * config.taker_fee_rate
        exit_slippage = (
            self.notional * config.slippage_bps / 10_000
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
        return data


class PaperBroker:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.balance = config.start_balance
        self.start_balance = config.start_balance
        self.positions: dict[str, Position] = {}
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
    def available_notional(self) -> float:
        cap = self.balance * self.config.max_leverage
        return max(0.0, cap - self.total_exposure)

    @property
    def available_risk_usd(self) -> float:
        cap = self.balance * self.config.max_total_risk_fraction
        return max(0.0, cap - self.open_risk_usd)

    def can_open(self, symbol: str) -> tuple[bool, str]:
        if symbol in self.positions:
            return False, "symbol already has an open position"
        if len(self.positions) >= self.config.max_open_positions:
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

    def open(self, plan: TradePlan, book: OrderBook) -> Position:
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
        slip = self.config.slippage_bps / 10_000
        fill = raw * (1 + slip if plan.side == Side.LONG else 1 - slip)
        fee = plan.notional * self.config.taker_fee_rate
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
            entry_fee_remaining=fee,
            last_price=fill,
            setup_id=plan.setup_id,
            strategy_details=dict(plan.strategy_details),
        )
        self.positions[plan.symbol] = position
        return position

    def mark(self, symbol: str, last_price: float, book: OrderBook) -> list[dict]:
        pos = self.positions.get(symbol)
        if pos is None:
            return []

        pos.last_price = last_price
        direction = 1 if pos.side == Side.LONG else -1
        executable = book.executable_exit(pos.side) or last_price

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
        if (
            self.config.partial_take_enabled
            and allow_runner
            and not pos.partial_taken
            and pos.mfe_r >= self.config.partial_take_at_r
        ):
            close_notional = self._partial_close_notional(pos)
            preview = self._preview_realize(
                pos,
                close_notional,
                book,
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
        final_leg = self._realize(pos, pos.notional, book)
        trade = {
            "event": "trade_closed",
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "side": pos.side.value,
            "setupId": pos.setup_id,
            "setupEntry": pos.setup_entry,
            "entry": pos.entry,
            "exit": final_leg["fill"],
            "initialStop": pos.initial_stop,
            "stop": pos.stop,
            "target": pos.target,
            "originalNotional": pos.original_notional,
            "grossPnl": pos.realized_gross_usd,
            "fees": pos.fees_paid_usd,
            "netPnl": pos.realized_net_usd,
            "partialTaken": pos.partial_taken,
            "mfeUsd": pos.mfe_usd,
            "maeUsd": pos.mae_usd,
            "mfeR": pos.mfe_r,
            "maeR": pos.mae_r,
            "initialRiskUsd": pos.initial_risk_usd,
            "reason": reason,
            "openedAt": pos.opened_at,
            "closedAt": time(),
            "strategyDetails": dict(pos.strategy_details),
        }
        self.total_closed_trades += 1
        self.closed_trades.append(trade)
        self.closed_trades = self.closed_trades[-200:]
        del self.positions[symbol]
        return trade

    def _partial_close_notional(self, pos: Position) -> float:
        return min(
            pos.notional,
            pos.original_notional
            * max(
                0.0,
                min(self.config.partial_take_fraction, 1.0),
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
        return max(
            0.0,
            float(economics.get("requiredNetProfitUsd") or 0.0),
        )

    def _runner_breakeven_stop(self, pos: Position) -> float:
        if pos.notional <= 0:
            return pos.entry
        exit_fee = pos.notional * self.config.taker_fee_rate
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
        slip = self.config.slippage_bps / 10_000

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
        )
        if preview["net"] < required_net:
            raise RuntimeError(
                "partial take attempted before economic threshold"
            )

        leg = self._realize(pos, close_notional, book)
        pos.partial_taken = True
        pos.partial_net_preview_usd = leg["net"]
        pos.partial_required_net_usd = required_net
        pos.partial_economic_ready = True

        risk_distance = abs(pos.entry - pos.initial_stop)
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
    ) -> dict:
        if close_notional <= 0 or pos.notional <= 0:
            return {
                "fill": pos.last_price,
                "gross": 0.0,
                "fees": 0.0,
                "net": 0.0,
            }

        close_notional = min(close_notional, pos.notional)
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
            levels = book.bids if pos.side == Side.LONG else book.asks
            if levels:
                worst = levels[-1][0]
                visible_base = visible_depth / raw if raw > 0 else 0.0
                missing = max(0.0, close_notional - visible_depth)
                total_base = visible_base + (
                    missing / worst if worst > 0 else 0.0
                )
                if total_base > 0:
                    raw = close_notional / total_base
        slip = self.config.slippage_bps / 10_000
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
        exit_fee = close_notional * self.config.taker_fee_rate
        fees = allocated_entry_fee + exit_fee
        return {
            "fill": fill,
            "gross": gross,
            "fees": fees,
            "net": gross - fees,
        }

    def _realize(self, pos: Position, close_notional: float, book: OrderBook) -> dict:
        if close_notional <= 0 or pos.notional <= 0:
            return {"fill": pos.last_price, "gross": 0.0, "fees": 0.0, "net": 0.0}

        close_notional = min(close_notional, pos.notional)
        leg = self._preview_realize(
            pos,
            close_notional,
            book,
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
        if age < self.config.no_follow_through_seconds or pos.initial_risk_usd <= 0:
            return False
        adverse_r = max(0.0, -gross_mark_original) / pos.initial_risk_usd
        return (
            pos.mfe_r < self.config.no_follow_through_max_mfe_r
            and adverse_r >= self.config.early_cut_at_r
        )
