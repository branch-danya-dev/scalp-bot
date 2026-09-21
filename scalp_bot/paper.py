from __future__ import annotations

from dataclasses import asdict, dataclass
from time import time

from .config import Settings
from .domain import OrderBook, Side, TradePlan


@dataclass(slots=True)
class Position:
    symbol: str
    strategy: str
    side: Side
    notional: float
    setup_entry: float
    entry: float
    stop: float
    target: float
    opened_at: float
    entry_fee: float
    last_price: float
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def initial_risk_usd(self) -> float:
        return self.notional * abs(self.entry - self.stop) / self.entry

    def public(self) -> dict:
        data = asdict(self)
        data["side"] = self.side.value
        data["initial_risk_usd"] = self.initial_risk_usd
        return data


class PaperBroker:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.balance = config.start_balance
        self.start_balance = config.start_balance
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[dict] = []

    @property
    def total_pnl(self) -> float:
        return self.balance - self.start_balance

    @property
    def total_exposure(self) -> float:
        return sum(x.notional for x in self.positions.values())

    @property
    def open_risk_usd(self) -> float:
        return sum(x.initial_risk_usd for x in self.positions.values())

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
        if self.total_pnl <= -(self.start_balance * self.config.max_daily_loss_fraction):
            return False, "daily loss limit reached"
        if self.available_notional <= 0:
            return False, "portfolio exposure budget exhausted"
        if self.available_risk_usd <= 0:
            return False, "portfolio risk budget exhausted"
        return True, "allowed"

    def open(self, plan: TradePlan, book: OrderBook) -> Position:
        allowed, reason = self.can_open(plan.symbol)
        if not allowed:
            raise RuntimeError(reason)
        raw = book.executable_entry(plan.side) or plan.market_entry
        slip = self.config.slippage_bps / 10_000
        fill = raw * (1 + slip if plan.side == Side.LONG else 1 - slip)
        fee = plan.notional * self.config.taker_fee_rate
        position = Position(
            symbol=plan.symbol,
            strategy=plan.strategy,
            side=plan.side,
            notional=plan.notional,
            setup_entry=plan.setup_entry,
            entry=fill,
            stop=plan.stop,
            target=plan.target,
            opened_at=time(),
            entry_fee=fee,
            last_price=fill,
        )
        self.positions[plan.symbol] = position
        return position

    def mark(self, symbol: str, last_price: float, book: OrderBook) -> dict | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        pos.last_price = last_price
        direction = 1 if pos.side == Side.LONG else -1
        gross_mark = direction * (last_price - pos.entry) / pos.entry * pos.notional
        pos.mfe_usd = max(pos.mfe_usd, gross_mark)
        pos.mae_usd = max(pos.mae_usd, -gross_mark)

        executable = book.executable_exit(pos.side) or last_price
        pos.unrealized_pnl = direction * (executable - pos.entry) / pos.entry * pos.notional
        pos.unrealized_pnl -= pos.entry_fee + pos.notional * self.config.taker_fee_rate

        hit_target = last_price >= pos.target if pos.side == Side.LONG else last_price <= pos.target
        hit_stop = last_price <= pos.stop if pos.side == Side.LONG else last_price >= pos.stop
        if not (hit_target or hit_stop):
            return None
        return self.close(symbol, book, "target" if hit_target else "stop")

    def close(self, symbol: str, book: OrderBook, reason: str) -> dict:
        pos = self.positions.get(symbol)
        if pos is None:
            raise RuntimeError("no paper position for symbol")
        raw = book.executable_exit(pos.side) or pos.last_price
        slip = self.config.slippage_bps / 10_000
        fill = raw * (1 - slip if pos.side == Side.LONG else 1 + slip)
        direction = 1 if pos.side == Side.LONG else -1
        gross = direction * (fill - pos.entry) / pos.entry * pos.notional
        exit_fee = pos.notional * self.config.taker_fee_rate
        net = gross - pos.entry_fee - exit_fee
        self.balance += net
        trade = {
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "side": pos.side.value,
            "setupEntry": pos.setup_entry,
            "entry": pos.entry,
            "exit": fill,
            "stop": pos.stop,
            "target": pos.target,
            "notional": pos.notional,
            "grossPnl": gross,
            "fees": pos.entry_fee + exit_fee,
            "netPnl": net,
            "mfeUsd": pos.mfe_usd,
            "maeUsd": pos.mae_usd,
            "initialRiskUsd": pos.initial_risk_usd,
            "reason": reason,
            "openedAt": pos.opened_at,
            "closedAt": time(),
        }
        self.closed_trades.append(trade)
        self.closed_trades = self.closed_trades[-200:]
        del self.positions[symbol]
        return trade
