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
class TradeTick:
    ts_ms: int
    price: float
    size: float
    side: str
    sequence: int = 0

    @property
    def notional(self) -> float:
        return self.price * self.size

    def public(self) -> dict[str, Any]:
        return {
            "ts": self.ts_ms,
            "price": self.price,
            "size": self.size,
            "side": self.side,
            "notional": self.notional,
            "sequence": self.sequence or None,
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

    @staticmethod
    def _vwap_for_notional(
        levels: list[tuple[float, float]],
        notional: float,
    ) -> tuple[float | None, float]:
        if notional <= 0:
            return None, 0.0
        remaining = notional
        filled_quote = 0.0
        filled_base = 0.0
        for price, qty in levels:
            if price <= 0 or qty <= 0:
                continue
            level_quote = price * qty
            take_quote = min(remaining, level_quote)
            filled_quote += take_quote
            filled_base += take_quote / price
            remaining -= take_quote
            if remaining <= max(1e-9, notional * 1e-9):
                break
        if filled_base <= 0:
            return None, 0.0
        return filled_quote / filled_base, filled_quote

    @staticmethod
    def _vwap_for_quantity(
        levels: list[tuple[float, float]],
        quantity: float,
    ) -> tuple[float | None, float, float]:
        if quantity <= 0:
            return None, 0.0, 0.0
        remaining = quantity
        filled_base = 0.0
        filled_quote = 0.0
        for price, qty in levels:
            if price <= 0 or qty <= 0:
                continue
            take_base = min(remaining, qty)
            filled_base += take_base
            filled_quote += take_base * price
            remaining -= take_base
            if remaining <= max(1e-12, quantity * 1e-12):
                break
        if filled_base <= 0:
            return None, 0.0, 0.0
        return (
            filled_quote / filled_base,
            filled_base,
            filled_quote,
        )

    def entry_vwap_quantity(
        self,
        side: Side,
        quantity: float,
    ) -> tuple[float | None, float, float]:
        levels = self.asks if side == Side.LONG else self.bids
        return self._vwap_for_quantity(levels, quantity)

    def exit_vwap_quantity(
        self,
        side: Side,
        quantity: float,
    ) -> tuple[float | None, float, float]:
        levels = self.bids if side == Side.LONG else self.asks
        return self._vwap_for_quantity(levels, quantity)

    def entry_vwap(
        self,
        side: Side,
        notional: float,
    ) -> tuple[float | None, float]:
        levels = self.asks if side == Side.LONG else self.bids
        return self._vwap_for_notional(levels, notional)

    def exit_vwap(
        self,
        side: Side,
        notional: float,
    ) -> tuple[float | None, float]:
        levels = self.bids if side == Side.LONG else self.asks
        return self._vwap_for_notional(levels, notional)

    def exit_vwap_from_trigger(
        self,
        side: Side,
        notional: float,
        trigger_price: float,
    ) -> tuple[float | None, float]:
        """VWAP only from levels that can remain beyond a stop trigger."""
        if trigger_price <= 0:
            return None, 0.0
        if side == Side.LONG:
            levels = [
                (price, qty)
                for price, qty in self.bids
                if price <= trigger_price
            ]
        else:
            levels = [
                (price, qty)
                for price, qty in self.asks
                if price >= trigger_price
            ]
        return self._vwap_for_notional(
            levels,
            notional,
        )

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
    volume_24h: float = 0.0
    spread_bps: float = 0.0
    top_book_notional_usd: float = 0.0
    trade_count_24h: int | None = None
    trade_count_source: str | None = None
    correlation_1h_btc: float | None = None
    activity_change: float = 0.0
    activity_turnover: float = 0.0
    activity_burst_ratio: float = 0.0
    activity_compression_ratio: float = 1.0
    activity_expansion_ratio: float = 1.0
    activity_move_spent_ratio: float = 0.0
    activity_level_proximity_score: float = 0.0
    opportunity_readiness: float = 0.0
    activity_score: float = 0.0
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
    details: dict[str, Any] = field(default_factory=dict)
    setup_id: str | None = None

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
    expected_net_loss: float
    net_reward_risk: float
    entry_drift_pct: float
    setup_id: str
    entry_mode: str = "taker_market"
    quantity: float | None = None
    strategy_details: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["side"] = self.side.value
        # These are deterministic outcomes at the configured target/stop,
        # not statistical expectancy. Keep legacy field names internally for
        # compatibility while exposing unambiguous public aliases.
        data["net_at_target"] = self.expected_net_profit
        data["net_at_stop"] = self.expected_net_loss
        return data
