from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..domain import OrderBook, TradeTick


@dataclass(slots=True)
class LevelFlow:
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    total_notional: float = 0.0
    imbalance: float = 0.0
    trade_count: int = 0
    price_response_pct: float = 0.0
    absorption_efficiency: float = 0.0

    def public(self) -> dict:
        return {
            "buyNotional": self.buy_notional,
            "sellNotional": self.sell_notional,
            "totalNotional": self.total_notional,
            "imbalance": self.imbalance,
            "tradeCount": self.trade_count,
            "priceResponsePct": self.price_response_pct,
            "absorptionEfficiency": self.absorption_efficiency,
        }


def prune_trades(
    trades: deque[TradeTick],
    now_ms: int,
    keep_seconds: int = 90,
) -> None:
    cutoff = now_ms - keep_seconds * 1000
    while trades and trades[0].ts_ms < cutoff:
        trades.popleft()


def cumulative_delta(
    trades: list[TradeTick],
    seconds: int,
    now_ms: int | None = None,
) -> float:
    if not trades:
        return 0.0
    now_ms = now_ms or trades[-1].ts_ms
    cutoff = now_ms - seconds * 1000
    buy = sum(
        t.notional
        for t in trades
        if cutoff <= t.ts_ms <= now_ms and t.side.lower() == "buy"
    )
    sell = sum(
        t.notional
        for t in trades
        if cutoff <= t.ts_ms <= now_ms and t.side.lower() == "sell"
    )
    return buy - sell


def flow_at_level(
    trades: list[TradeTick],
    level_price: float,
    *,
    tolerance_pct: float = 0.0008,
    seconds: int = 15,
    now_ms: int | None = None,
) -> LevelFlow:
    if not trades or level_price <= 0:
        return LevelFlow()
    now_ms = now_ms or trades[-1].ts_ms
    cutoff = now_ms - seconds * 1000
    rows = [
        t
        for t in trades
        if cutoff <= t.ts_ms <= now_ms
        and abs(t.price - level_price) / level_price <= tolerance_pct
    ]
    if not rows:
        return LevelFlow()
    buy = sum(t.notional for t in rows if t.side.lower() == "buy")
    sell = sum(t.notional for t in rows if t.side.lower() == "sell")
    total = buy + sell
    first = rows[0].price
    last = rows[-1].price
    response = (last - first) / first if first > 0 else 0.0
    imbalance = (buy - sell) / total if total > 0 else 0.0
    aggression = abs(imbalance)
    response_norm = min(1.0, abs(response) / max(tolerance_pct, 1e-9))
    absorption = max(0.0, aggression * (1.0 - response_norm))
    return LevelFlow(
        buy_notional=buy,
        sell_notional=sell,
        total_notional=total,
        imbalance=imbalance,
        trade_count=len(rows),
        price_response_pct=response,
        absorption_efficiency=absorption,
    )


def flow_beyond_level(
    trades: list[TradeTick],
    boundary_price: float,
    *,
    long_side: bool,
    seconds: int = 5,
    now_ms: int | None = None,
) -> LevelFlow:
    """Executed flow that actually occurred beyond a broken level edge."""
    if not trades or boundary_price <= 0:
        return LevelFlow()
    now_ms = now_ms or trades[-1].ts_ms
    cutoff = now_ms - seconds * 1000
    rows = [
        trade
        for trade in trades
        if cutoff <= trade.ts_ms <= now_ms
        and (
            trade.price >= boundary_price
            if long_side
            else trade.price <= boundary_price
        )
    ]
    if not rows:
        return LevelFlow()
    buy = sum(t.notional for t in rows if t.side.lower() == "buy")
    sell = sum(t.notional for t in rows if t.side.lower() == "sell")
    total = buy + sell
    first = rows[0].price
    last = rows[-1].price
    response = (last - first) / first if first > 0 else 0.0
    return LevelFlow(
        buy_notional=buy,
        sell_notional=sell,
        total_notional=total,
        imbalance=(buy - sell) / total if total > 0 else 0.0,
        trade_count=len(rows),
        price_response_pct=response,
    )


def best_level_ofi_usd(previous: OrderBook, current: OrderBook) -> float:
    """Best-level order-flow imbalance in quote notional.

    Positive values mean net bid-side pressure / ask withdrawal; negative
    values mean net ask-side pressure / bid withdrawal. This is the standard
    best-level OFI event construction expressed in quote notional.
    """
    if (
        not previous.bids
        or not previous.asks
        or not current.bids
        or not current.asks
    ):
        return 0.0

    prev_bid_price, prev_bid_qty = previous.bids[0]
    bid_price, bid_qty = current.bids[0]
    prev_ask_price, prev_ask_qty = previous.asks[0]
    ask_price, ask_qty = current.asks[0]

    bid = 0.0
    if bid_price >= prev_bid_price:
        bid += bid_price * bid_qty
    if bid_price <= prev_bid_price:
        bid -= prev_bid_price * prev_bid_qty

    ask = 0.0
    if ask_price <= prev_ask_price:
        ask -= ask_price * ask_qty
    if ask_price >= prev_ask_price:
        ask += prev_ask_price * prev_ask_qty

    return bid + ask
