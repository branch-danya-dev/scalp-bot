from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import httpx
import websockets

from .config import Settings
from .domain import Candidate, Candle, OrderBook


class BybitError(RuntimeError):
    pass


class BybitRestClient:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.bybit_rest_url, timeout=10.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict[str, str | int]) -> dict:
        response = await self.client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise BybitError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        return payload["result"]

    async def liquid_candidates(self, limit: int | None = None) -> list[Candidate]:
        result = await self._get("/v5/market/tickers", {"category": "linear"})
        rows: list[Candidate] = []
        for item in result.get("list", []):
            symbol = item.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            turnover = float(item.get("turnover24h") or 0)
            if turnover < self.config.min_turnover_usd:
                continue
            rows.append(
                Candidate(
                    symbol=symbol,
                    turnover_24h=turnover,
                    change_24h=float(item.get("price24hPcnt") or 0),
                    last_price=float(item.get("lastPrice") or 0),
                )
            )
        rows.sort(key=lambda x: x.turnover_24h, reverse=True)
        return rows[:limit] if limit else rows

    async def active_candidates(self) -> list[Candidate]:
        liquid = await self.liquid_candidates(self.config.liquid_universe_size)
        semaphore = asyncio.Semaphore(max(1, self.config.activity_request_concurrency))

        async def enrich(candidate: Candidate) -> Candidate:
            async with semaphore:
                candles = await self.klines(
                    candidate.symbol,
                    "1",
                    max(self.config.activity_window_minutes + 1, 3),
                )
                await asyncio.sleep(self.config.activity_request_pause_seconds)
            if len(candles) >= 2:
                first = candles[0]
                last = candles[-1]
                if first.open:
                    candidate.activity_change = (last.close - first.open) / first.open
                candidate.activity_turnover = sum(x.turnover for x in candles[-self.config.activity_window_minutes :])
            return candidate

        enriched = await asyncio.gather(*(enrich(x) for x in liquid), return_exceptions=True)
        rows: list[Candidate] = []
        for original, item in zip(liquid, enriched, strict=True):
            rows.append(original if isinstance(item, Exception) else item)

        rows.sort(key=lambda x: (abs(x.activity_change), x.activity_turnover), reverse=True)
        for index, item in enumerate(rows, start=1):
            item.activity_rank = index
        return rows

    async def klines(self, symbol: str, interval: str, limit: int = 240) -> list[Candle]:
        result = await self._get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
        )
        candles: list[Candle] = []
        for row in reversed(result.get("list", [])):
            candles.append(
                Candle(
                    start_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[6]),
                    confirmed=True,
                )
            )
        return candles


class OrderBookState:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply(self, message: dict) -> OrderBook:
        data = message.get("data") or {}
        if message.get("type") == "snapshot":
            self.bids.clear()
            self.asks.clear()

        self._apply_side(self.bids, data.get("b", []))
        self._apply_side(self.asks, data.get("a", []))

        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:50]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:50]
        return OrderBook(bids=bids, asks=asks)

    @staticmethod
    def _apply_side(side: dict[float, float], changes: list[list[str]]) -> None:
        for price_raw, qty_raw in changes:
            price, qty = float(price_raw), float(qty_raw)
            if qty == 0:
                side.pop(price, None)
            else:
                side[price] = qty


StreamCallback = Callable[[dict], Awaitable[None]]


async def stream_symbol(
    ws_url: str,
    symbol: str,
    callback: StreamCallback,
    stop_event: asyncio.Event,
) -> None:
    topics = [f"orderbook.50.{symbol}", f"kline.1.{symbol}", f"publicTrade.{symbol}"]
    while not stop_event.is_set():
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                while not stop_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=35)
                    message = json.loads(raw)
                    if "topic" in message:
                        await callback(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not stop_event.is_set():
                await asyncio.sleep(2)
