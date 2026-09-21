from __future__ import annotations

from dataclasses import asdict, dataclass
from time import time

from .config import Settings
from .domain import Side, TradePlan


@dataclass(slots=True)
class Position:
    symbol: str
    strategy: str
    side: Side
    notional: float
    entry: float
    stop: float
    target: float
    opened_at: float
    entry_fee: float

    def public(self) -> dict:
        data = asdict(self)
        data["side"] = self.side.value
        return data


class PaperBroker:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.balance = config.start_balance
        self.start_balance = config.start_balance
        self.position: Position | None = None
        self.closed_trades: list[dict] = []

    @property
    def total_pnl(self) -> float:
        return self.balance - self.start_balance

    def open(self, plan: TradePlan, spread_pct: float) -> Position:
        if self.position is not None:
            raise RuntimeError("only one paper position is supported in MVP")
        direction = 1 if plan.side == Side.LONG else -1
        friction = spread_pct / 2 + self.config.slippage_bps / 10_000
        fill = plan.entry * (1 + direction * friction)
        fee = plan.notional * self.config.taker_fee_rate
        self.position = Position(
            symbol=plan.symbol,
            strategy=plan.strategy,
            side=plan.side,
            notional=plan.notional,
            entry=fill,
            stop=plan.stop,
            target=plan.target,
            opened_at=time(),
            entry_fee=fee,
        )
        return self.position

    def mark(self, symbol: str, price: float, spread_pct: float) -> dict | None:
        pos = self.position
        if pos is None or pos.symbol != symbol:
            return None
        hit_target = price >= pos.target if pos.side == Side.LONG else price <= pos.target
        hit_stop = price <= pos.stop if pos.side == Side.LONG else price >= pos.stop
        if not (hit_target or hit_stop):
            return None
        return self.close(price, spread_pct, "target" if hit_target else "stop")

    def close(self, raw_price: float, spread_pct: float, reason: str) -> dict:
        pos = self.position
        if pos is None:
            raise RuntimeError("no paper position")
        direction = 1 if pos.side == Side.LONG else -1
        friction = spread_pct / 2 + self.config.slippage_bps / 10_000
        fill = raw_price * (1 - direction * friction)
        gross = direction * (fill - pos.entry) / pos.entry * pos.notional
        exit_fee = pos.notional * self.config.taker_fee_rate
        net = gross - pos.entry_fee - exit_fee
        self.balance += net
        trade = {
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "side": pos.side.value,
            "entry": pos.entry,
            "exit": fill,
            "stop": pos.stop,
            "target": pos.target,
            "notional": pos.notional,
            "grossPnl": gross,
            "fees": pos.entry_fee + exit_fee,
            "netPnl": net,
            "reason": reason,
            "openedAt": pos.opened_at,
            "closedAt": time(),
        }
        self.closed_trades.append(trade)
        self.closed_trades = self.closed_trades[-100:]
        self.position = None
        return trade
