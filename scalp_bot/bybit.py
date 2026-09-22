from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import time

import httpx
import websockets

from .activity import activity_score, correlation_1h
from .config import Settings
from .domain import Candidate, Candle, OrderBook


class BybitError(RuntimeError):
    pass


class OrderBookSequenceError(RuntimeError):
    pass


def _interval_ms(interval: str) -> int | None:
    if interval.isdigit():
        return int(interval) * 60_000
    return {"D": 24 * 60 * 60_000, "W": 7 * 24 * 60_000}.get(interval)


def _kline_is_confirmed(
    start_ms: int,
    interval: str,
    *,
    now_ms: int | None = None,
) -> bool:
    duration = _interval_ms(interval)
    if duration is None:
        return True
    resolved_now = int(time() * 1000) if now_ms is None else now_ms
    return start_ms + duration <= resolved_now


class BybitRestClient:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.bybit_rest_url, timeout=10.0)
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def _pace_request(self) -> None:
        minimum = max(0.0, self.config.rest_request_min_interval_seconds)
        if minimum <= 0:
            return
        async with self._request_lock:
            now = asyncio.get_running_loop().time()
            wait = minimum - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = asyncio.get_running_loop().time()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass

        base = max(0.05, self.config.rest_rate_limit_backoff_seconds)
        maximum = max(base, self.config.rest_rate_limit_max_backoff_seconds)
        return min(maximum, base * (2 ** attempt))

    async def _get(self, path: str, params: dict[str, str | int]) -> dict:
        retries = max(0, self.config.rest_rate_limit_retries)

        for attempt in range(retries + 1):
            await self._pace_request()
            response = await self.client.get(path, params=params)

            if response.status_code == 429:
                if attempt >= retries:
                    response.raise_for_status()
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            response.raise_for_status()
            payload = response.json()
            code = payload.get("retCode")
            if code == 0:
                return payload["result"]

            if code == 10006 and attempt < retries:
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            raise BybitError(f"Bybit error {code}: {payload.get('retMsg')}")

        raise BybitError("Bybit REST retry loop exhausted")

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
            bid = float(item.get("bid1Price") or 0)
            ask = float(item.get("ask1Price") or 0)
            bid_size = float(item.get("bid1Size") or 0)
            ask_size = float(item.get("ask1Size") or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2
            spread_bps = (
                (ask - bid) / mid * 10_000
                if mid > 0
                else float("inf")
            )
            # All strategies form entries near the current mid. If half the
            # ticker spread alone already exceeds max_entry_drift_bps, the
            # best executable quote cannot satisfy the configured chase limit.
            if spread_bps > self.config.max_entry_drift_bps * 2:
                continue
            top_book_notional = min(
                bid * max(bid_size, 0.0),
                ask * max(ask_size, 0.0),
            )
            rows.append(
                Candidate(
                    symbol=symbol,
                    turnover_24h=turnover,
                    change_24h=float(item.get("price24hPcnt") or 0),
                    last_price=float(item.get("lastPrice") or 0),
                    volume_24h=float(item.get("volume24h") or 0),
                    spread_bps=spread_bps,
                    top_book_notional_usd=top_book_notional,
                    trade_count_24h=None,
                    trade_count_source="not_available_from_bybit_v5_ticker",
                )
            )
        rows.sort(key=lambda x: x.turnover_24h, reverse=True)
        return rows[:limit] if limit else rows

    async def active_candidates(self) -> list[Candidate]:
        liquid = await self.liquid_candidates(self.config.liquid_universe_size)
        semaphore = asyncio.Semaphore(max(1, self.config.activity_request_concurrency))
        correlation_limit = max(
            self.config.activity_correlation_window_minutes + 1,
            21,
        )
        benchmark = await self.klines(
            self.config.activity_benchmark_symbol,
            "1",
            correlation_limit,
        )
        benchmark_closed = [x for x in benchmark if x.confirmed]

        async def enrich(candidate: Candidate) -> Candidate:
            async with semaphore:
                candles = await self.klines(
                    candidate.symbol,
                    "1",
                    max(correlation_limit, self.config.activity_window_minutes + 1),
                )
                await asyncio.sleep(self.config.activity_request_pause_seconds)
            closed = [x for x in candles if x.confirmed]
            if len(closed) >= 2:
                window = max(self.config.activity_window_minutes, 1)
                recent = closed[-max(window + 1, 2):]
                first = recent[0]
                last = recent[-1]
                if first.open:
                    candidate.activity_change = (last.close - first.open) / first.open
                recent_rows = closed[-window:]
                candidate.activity_turnover = sum(x.turnover for x in recent_rows)
                baseline_rows = closed[:-window]
                if baseline_rows and candidate.activity_turnover > 0:
                    baseline_per_minute = sum(x.turnover for x in baseline_rows) / len(baseline_rows)
                    expected_recent = baseline_per_minute * len(recent_rows)
                    if expected_recent > 0:
                        candidate.activity_burst_ratio = candidate.activity_turnover / expected_recent
                candidate.correlation_1h_btc = (
                    1.0
                    if candidate.symbol == self.config.activity_benchmark_symbol
                    else correlation_1h(closed, benchmark_closed)
                )
                candidate.activity_score = activity_score(
                    candidate,
                    self.config.activity_window_minutes,
                )
            return candidate

        enriched = await asyncio.gather(
            *(enrich(x) for x in liquid),
            return_exceptions=True,
        )
        rows: list[Candidate] = []
        for original, item in zip(liquid, enriched, strict=True):
            rows.append(original if isinstance(item, Exception) else item)

        rows.sort(
            key=lambda x: (
                x.activity_score,
                abs(x.activity_change),
                x.activity_turnover,
            ),
            reverse=True,
        )
        for index, item in enumerate(rows, start=1):
            item.activity_rank = index
        return rows

    async def klines(self, symbol: str, interval: str, limit: int = 240) -> list[Candle]:
        result = await self._get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
        )
        candles: list[Candle] = []
        now_ms = int(time() * 1000)
        for row in reversed(result.get("list", [])):
            start_ms = int(row[0])
            candles.append(
                Candle(
                    start_ms=start_ms,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[6]),
                    confirmed=_kline_is_confirmed(start_ms, interval, now_ms=now_ms),
                )
            )
        return candles


class OrderBookState:
    def __init__(self, depth: int = 200) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.depth = depth
        self.last_update_id: int | None = None
        self.last_seq: int | None = None
        self.synced = False

    def _clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.last_seq = None
        self.synced = False

    def _book(self) -> OrderBook:
        bids = sorted(
            self.bids.items(),
            key=lambda x: x[0],
            reverse=True,
        )[: self.depth]
        asks = sorted(
            self.asks.items(),
            key=lambda x: x[0],
        )[: self.depth]
        return OrderBook(bids=bids, asks=asks)

    def apply(self, message: dict) -> OrderBook:
        data = message.get("data") or {}
        update_id = int(data.get("u") or 0)
        seq = int(data.get("seq") or 0)
        is_snapshot = message.get("type") == "snapshot" or update_id == 1

        if is_snapshot:
            self._clear()
            self._apply_side(self.bids, data.get("b", []))
            self._apply_side(self.asks, data.get("a", []))
            self.last_update_id = update_id or None
            self.last_seq = seq or None
            self.synced = True
            return self._book()

        if not self.synced:
            raise OrderBookSequenceError(
                "orderbook delta received before a fresh snapshot"
            )

        if (
            self.last_update_id is not None
            and update_id
            and update_id <= self.last_update_id
        ):
            return self._book()

        if (
            self.last_seq is not None
            and seq
            and seq < self.last_seq
        ):
            return self._book()

        if (
            self.last_update_id is not None
            and update_id
            and update_id > self.last_update_id + 1
        ):
            expected = self.last_update_id + 1
            self._clear()
            raise OrderBookSequenceError(
                f"orderbook gap: expected u={expected}, got u={update_id}"
            )

        self._apply_side(self.bids, data.get("b", []))
        self._apply_side(self.asks, data.get("a", []))
        if update_id:
            self.last_update_id = update_id
        if seq:
            self.last_seq = seq
        return self._book()

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
    orderbook_depth: int = 1000,
) -> None:
    if orderbook_depth not in {1, 50, 200, 1000}:
        raise ValueError(
            "Bybit orderbook depth must be one of 1, 50, 200, 1000"
        )
    topics = [
        f"orderbook.{orderbook_depth}.{symbol}",
        f"kline.1.{symbol}",
        f"publicTrade.{symbol}",
    ]
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
