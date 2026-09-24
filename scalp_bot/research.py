from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from .domain import Candle, OrderBook, TradeTick, Trend
from .strategy import create_default_strategies
from .strategy.structure import market_structure_from_public


@dataclass(slots=True)
class ShadowSignal:
    ts: float
    symbol: str
    strategy: str
    action: str
    entry: float
    stop: float
    target: float
    quality: float
    setup_id: str | None

    def public(self) -> dict:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "action": self.action,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "quality": self.quality,
            "setupId": self.setup_id,
        }


def _candle_from_public(row: dict) -> Candle:
    return Candle(
        start_ms=int(row["time"]) * 1000,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume") or 0),
        turnover=float(row.get("turnover") or 0),
        confirmed=bool(row.get("confirmed", True)),
    )


def _book_from_public(row: dict) -> OrderBook:
    return OrderBook(
        bids=[(float(x[0]), float(x[1])) for x in row.get("bids") or []],
        asks=[(float(x[0]), float(x[1])) for x in row.get("asks") or []],
    )


def _trades_from_public(rows: list[dict]) -> list[TradeTick]:
    return [
        TradeTick(
            ts_ms=int(row["ts"]),
            price=float(row["price"]),
            size=float(row["size"]),
            side=str(row["side"]),
            sequence=int(row.get("sequence") or 0),
        )
        for row in rows
    ]


class OfflineStrategyReplay:
    def __init__(self) -> None:
        self.strategies = {
            strategy.key: strategy
            for strategy in create_default_strategies()
        }

    def run_rows(self, rows: list[dict], *, symbol: str | None = None) -> list[ShadowSignal]:
        candles_by_symbol: dict[str, list[Candle]] = defaultdict(list)
        trades_by_symbol: dict[
            str,
            deque[TradeTick],
        ] = defaultdict(deque)
        signals: list[ShadowSignal] = []
        for row in rows:
            event = row.get("event")
            row_symbol = row.get("symbol")
            if not row_symbol or (symbol is not None and row_symbol != symbol):
                continue
            payload = row.get("payload") or {}
            if event == "symbol_activated":
                market = payload.get("market") or {}
                candles_by_symbol[row_symbol] = [
                    _candle_from_public(c) for c in market.get("candles") or []
                ]
                continue
            if event != "research_frame":
                continue
            candle_row = payload.get("candle")
            if candle_row:
                candle = _candle_from_public(candle_row)
                candles = candles_by_symbol[row_symbol]
                if candles and candles[-1].start_ms == candle.start_ms:
                    candles[-1] = candle
                else:
                    candles.append(candle)
                candles_by_symbol[row_symbol] = candles[-720:]
            candles = candles_by_symbol[row_symbol]
            closed_candles = [
                candle
                for candle in candles
                if candle.confirmed
            ]
            if not closed_candles:
                continue
            fast_book = _book_from_public(
                payload.get("fastOrderbook")
                or payload.get("orderbook")
                or {}
            )
            deep_book = _book_from_public(
                payload.get("deepOrderbook")
                or payload.get("orderbook")
                or {}
            )
            if not fast_book.bids or not fast_book.asks:
                continue
            frame_trades = _trades_from_public(
                payload.get("recentTrades") or []
            )
            trade_encoding = str(
                payload.get("tradeEncoding") or "rolling_v1"
            )
            if trade_encoding == "delta_v1":
                buffer = trades_by_symbol[row_symbol]
                if bool(payload.get("tradeDeltaGap")):
                    buffer.clear()
                seen_sequences = {
                    trade.sequence
                    for trade in buffer
                    if trade.sequence > 0
                }
                for trade in frame_trades:
                    if (
                        trade.sequence > 0
                        and trade.sequence in seen_sequences
                    ):
                        continue
                    buffer.append(trade)
                    if trade.sequence > 0:
                        seen_sequences.add(trade.sequence)
                observed_at_ms = int(
                    float(row.get("ts") or 0) * 1000
                )
                cutoff = observed_at_ms - 90_000
                while (
                    buffer
                    and buffer[0].ts_ms < cutoff
                ):
                    buffer.popleft()
                trades = list(buffer)
            else:
                trades = frame_trades
                trades_by_symbol[row_symbol] = deque(
                    frame_trades
                )
            try:
                trend = Trend(str(payload.get("trend") or "flat"))
            except ValueError:
                trend = Trend.FLAT
            structure = market_structure_from_public(payload.get("structure"))
            for strategy in self.strategies.values():
                observed_at_ms = int(float(row.get("ts") or 0) * 1000)
                strategy_book = (
                    deep_book
                    if strategy.key == "orderbook_density"
                    and deep_book.bids
                    and deep_book.asks
                    else fast_book
                )
                decision = strategy.evaluate(
                    closed_candles,
                    strategy_book,
                    trend,
                    symbol=row_symbol,
                    trades=trades,
                    structure=structure,
                    observed_at_ms=(
                        observed_at_ms if observed_at_ms > 0 else None
                    ),
                )
                if not decision.tradeable:
                    continue
                signals.append(ShadowSignal(
                    ts=float(row.get("ts") or 0),
                    symbol=row_symbol,
                    strategy=decision.strategy,
                    action=decision.action.value,
                    entry=float(decision.entry),
                    stop=float(decision.stop),
                    target=float(decision.target),
                    quality=float(decision.details.get("setupQuality", decision.confidence) or 0.0),
                    setup_id=decision.setup_id,
                ))
        return signals


def load_session(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
