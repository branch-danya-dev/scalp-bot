from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class Action(StrEnum):
    WAIT = "wait"
    LONG = "long"
    SHORT = "short"


class Trend(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


@dataclass(slots=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    confirmed: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "time": self.start_ms // 1000,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "turnover": self.turnover,
            "confirmed": self.confirmed,
        }


@dataclass(slots=True)
class OrderBook:
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_ask + self.best_bid) / 2

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if not mid or self.best_bid is None or self.best_ask is None:
            return 0.0
        return (self.best_ask - self.best_bid) / mid

    def executable_entry(self, side: Side) -> float | None:
        return self.best_ask if side == Side.LONG else self.best_bid

    def executable_exit(self, side: Side) -> float | None:
        return self.best_bid if side == Side.LONG else self.best_ask

    def public(self, depth: int = 16) -> dict[str, Any]:
        return {
            "bids": [[p, q, p * q] for p, q in self.bids[:depth]],
            "asks": [[p, q, p * q] for p, q in self.asks[:depth]],
            "bestBid": self.best_bid,
            "bestAsk": self.best_ask,
            "spreadPct": self.spread_pct,
        }


@dataclass(slots=True)
class Candidate:
    symbol: str
    turnover_24h: float
    change_24h: float
    last_price: float
    activity_change: float = 0.0
    activity_turnover: float = 0.0
    activity_rank: int | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StrategyDecision:
    strategy: str
    action: Action
    reasons: list[str]
    confidence: float = 0.0
    watched_level: float | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    visuals: dict[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> Side | None:
        if self.action == Action.LONG:
            return Side.LONG
        if self.action == Action.SHORT:
            return Side.SHORT
        return None

    @property
    def tradeable(self) -> bool:
        return self.side is not None and None not in (self.entry, self.stop, self.target)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass(slots=True)
class TradePlan:
    symbol: str
    strategy: str
    side: Side
    setup_entry: float
    market_entry: float
    stop: float
    target: float
    notional: float
    leverage: float
    max_loss_usd: float
    expected_gross_profit: float
    estimated_costs: float
    expected_net_profit: float
    entry_drift_pct: float

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["side"] = self.side.value
        return data
