from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..domain import TradeTick


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
        if t.ts_ms >= cutoff and t.side.lower() == "buy"
    )
    sell = sum(
        t.notional
        for t in trades
        if t.ts_ms >= cutoff and t.side.lower() == "sell"
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
        if t.ts_ms >= cutoff
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
