from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from itertools import count
from time import perf_counter_ns, time, time_ns
from typing import Any

import httpx
import msgspec
import websockets
from opentelemetry import trace

from .activity import (
    activity_score,
    correlation_1h,
    opportunity_readiness,
)
from .config import Settings
from .domain import Candidate, Candle, InstrumentSpec, OrderBook
from .latency_observability import (
    exchange_receive_seconds,
    observe_latency,
    observe_market_queue,
    span,
    stream_name,
    tracer,
)


class BybitError(RuntimeError):
    pass


class OrderBookSequenceError(RuntimeError):
    pass


class MarketDataBackpressureError(RuntimeError):
    pass


class MarketMessage(msgspec.Struct):
    topic: str | None = None
    type: str | None = None
    ts: int | None = None
    cts: int | None = None
    data: Any = None
    success: bool | None = None
    op: str | None = None
    # Runtime-only ingest telemetry. These fields are absent on the wire and
    # populated after decoding.
    received_at_ns: int = 0
    queue_depth: int = 0
    queue_lag_ms: float = 0.0
    event_id: str | None = None
    trace_id: str | None = None
    receipt_wall_ns: int = 0
    receipt_mono_ns: int = 0
    parsed_mono_ns: int = 0
    processor_started_mono_ns: int = 0
    book_updated_mono_ns: int = 0
    features_ready_mono_ns: int = 0
    strategy_eval_started_mono_ns: int = 0
    strategy_eval_finished_mono_ns: int = 0
    fire_mono_ns: int = 0
    order_sent_mono_ns: int = 0
    order_ack_mono_ns: int = 0
    fill_mono_ns: int = 0
    otel_span: Any = None

    def get(self, key: str, default=None):
        return getattr(self, key, default)


_MARKET_DECODER = msgspec.json.Decoder(
    type=MarketMessage,
    strict=False,
)
_JSON_ENCODER = msgspec.json.Encoder()
_MARKET_EVENT_IDS = count(1)


def decode_market_message(raw: bytes | str) -> MarketMessage:
    return _MARKET_DECODER.decode(raw)


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
        configured_urls = [
            config.bybit_rest_url,
            *[
                row.strip()
                for row in config.bybit_rest_fallback_urls.split(",")
                if row.strip()
            ],
        ]
        self._rest_urls = list(dict.fromkeys(
            row.rstrip("/")
            for row in configured_urls
            if row
        ))
        self._active_rest_url = self._rest_urls[0]
        self.client = httpx.AsyncClient(timeout=10.0)
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def active_rest_url(self) -> str:
        return self._active_rest_url

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

    @staticmethod
    def _response_excerpt(response: httpx.Response) -> str:
        text = (response.text or "").replace("\n", " ").strip()
        return text[:240] or "<empty response>"

    async def _get(self, path: str, params: dict[str, str | int]) -> dict:
        retries = max(0, self.config.rest_rate_limit_retries)
        endpoints = [
            self._active_rest_url,
            *[
                url
                for url in self._rest_urls
                if url != self._active_rest_url
            ],
        ]
        endpoint_errors: list[str] = []

        for base_url in endpoints:
            for attempt in range(retries + 1):
                await self._pace_request()
                try:
                    response = await self.client.get(
                        f"{base_url}{path}",
                        params=params,
                    )
                except httpx.RequestError as exc:
                    endpoint_errors.append(
                        f"{base_url}: {type(exc).__name__}: {exc}"
                    )
                    break

                if response.status_code == 403:
                    endpoint_errors.append(
                        (
                            f"{base_url}: HTTP 403: "
                            f"{self._response_excerpt(response)}"
                        )
                    )
                    break

                if response.status_code == 429:
                    if attempt >= retries:
                        endpoint_errors.append(
                            (
                                f"{base_url}: HTTP 429: "
                                f"{self._response_excerpt(response)}"
                            )
                        )
                        break
                    await asyncio.sleep(
                        self._retry_delay(response, attempt)
                    )
                    continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise BybitError(
                        (
                            f"Bybit HTTP {response.status_code} at "
                            f"{base_url}: "
                            f"{self._response_excerpt(response)}"
                        )
                    ) from exc

                payload = response.json()
                code = payload.get("retCode")
                if code == 0:
                    self._active_rest_url = base_url
                    return payload["result"]

                if code == 10006 and attempt < retries:
                    await asyncio.sleep(
                        self._retry_delay(response, attempt)
                    )
                    continue

                raise BybitError(
                    f"Bybit error {code}: {payload.get('retMsg')}"
                )

        detail = " | ".join(endpoint_errors) or "no endpoint response"
        raise BybitError(
            (
                "Bybit Global REST is unavailable from the current "
                f"connection. Tried: {detail}. "
                "HTTP 403 may be caused by an IP rate block or a "
                "regional access restriction. This bot uses the Global "
                "linear perpetual market and will not silently switch to "
                "Bybit EU Spot/Spot Margin."
            )
        )

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
                (
                    candidate.opportunity_readiness,
                    candidate.activity_compression_ratio,
                    candidate.activity_expansion_ratio,
                    candidate.activity_move_spent_ratio,
                    candidate.activity_level_proximity_score,
                ) = opportunity_readiness(closed)
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

    async def instrument_spec(self, symbol: str) -> InstrumentSpec:
        result = await self._get(
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
        )
        rows = result.get("list") or []
        if not rows:
            raise BybitError(
                f"instrument metadata is unavailable for {symbol}"
            )
        item = rows[0]
        price_filter = item.get("priceFilter") or {}
        lot_filter = item.get("lotSizeFilter") or {}
        return InstrumentSpec(
            symbol=symbol,
            status=str(item.get("status") or ""),
            tick_size=float(price_filter.get("tickSize") or 0.0),
            qty_step=float(lot_filter.get("qtyStep") or 0.0),
            min_order_qty=float(
                lot_filter.get("minOrderQty") or 0.0
            ),
            min_notional_value=float(
                lot_filter.get("minNotionalValue") or 0.0
            ),
        )

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


StreamCallback = Callable[
    [MarketMessage],
    Awaitable[None],
]


async def _process_market_queue(
    queue: asyncio.Queue[MarketMessage],
    callback: StreamCallback,
    stop_event: asyncio.Event,
    *,
    max_lag_seconds: float,
) -> None:
    while not stop_event.is_set():
        message = await queue.get()
        active_root = message.otel_span
        context = (
            trace.use_span(
                active_root,
                end_on_exit=False,
            )
            if active_root is not None
            else nullcontext()
        )
        try:
            with context:
                message.queue_depth = queue.qsize()
                message.processor_started_mono_ns = (
                    perf_counter_ns()
                )
                queue_anchor_ns = int(
                    message.parsed_mono_ns
                    or message.received_at_ns
                    or 0
                )
                lag_seconds = (
                    max(
                        0.0,
                        (
                            message.processor_started_mono_ns
                            - queue_anchor_ns
                        )
                        / 1_000_000_000,
                    )
                    if queue_anchor_ns > 0
                    else 0.0
                )
                message.queue_lag_ms = (
                    lag_seconds * 1000
                )
                stream = stream_name(message.topic)
                observe_market_queue(
                    stream=stream,
                    symbol=(
                        str(message.topic or "").split(".")[-1]
                        if message.topic
                        else ""
                    ),
                    depth=message.queue_depth,
                    lag_seconds=lag_seconds,
                )
                observe_latency(
                    "parse_to_processor",
                    lag_seconds,
                    stream=stream,
                )
                if (
                    max_lag_seconds > 0
                    and lag_seconds > max_lag_seconds
                ):
                    observe_latency(
                        "parse_to_processor",
                        lag_seconds,
                        stream=stream,
                        status="stale",
                    )
                    raise MarketDataBackpressureError(
                        "market processor lag exceeded "
                        f"{max_lag_seconds:.3f}s "
                        f"(lag={lag_seconds:.3f}s, "
                        f"queue={message.queue_depth})"
                    )
                with span(
                    "market.process",
                    **{
                        "market.event_id": message.event_id,
                        "market.topic": message.topic,
                        "market.queue_depth": message.queue_depth,
                        "market.queue_lag_ms": message.queue_lag_ms,
                    },
                ):
                    await callback(message)
        finally:
            if active_root is not None:
                active_root.end()
            queue.task_done()


async def _stream_topics(
    ws_url: str,
    topics: list[str],
    callback: StreamCallback,
    stop_event: asyncio.Event,
    *,
    queue_size: int = 512,
    queue_put_timeout_seconds: float = 0.05,
    queue_max_lag_seconds: float = 0.50,
) -> None:
    while not stop_event.is_set():
        processor: asyncio.Task | None = None
        queue: asyncio.Queue[MarketMessage] | None = None
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                await ws.send(
                    _JSON_ENCODER.encode({
                        "op": "subscribe",
                        "args": topics,
                    }),
                    text=True,
                )
                queue = asyncio.Queue(
                    maxsize=max(1, int(queue_size)),
                )
                processor = asyncio.create_task(
                    _process_market_queue(
                        queue,
                        callback,
                        stop_event,
                        max_lag_seconds=max(
                            0.0,
                            queue_max_lag_seconds,
                        ),
                    ),
                    name=(
                        "market-processor:"
                        + ",".join(topics)
                    ),
                )
                while not stop_event.is_set():
                    if processor.done():
                        exc = processor.exception()
                        if exc is not None:
                            raise exc
                        raise RuntimeError(
                            "market processor stopped unexpectedly"
                        )

                    raw = await asyncio.wait_for(
                        ws.recv(decode=False),
                        timeout=35,
                    )
                    # Receipt is captured immediately after recv returns so
                    # socket wait time isn't counted as parser work.
                    receipt_wall_ns = time_ns()
                    receipt_mono_ns = perf_counter_ns()
                    message = decode_market_message(raw)
                    parsed_mono_ns = perf_counter_ns()
                    if not message.topic:
                        continue

                    message.receipt_wall_ns = receipt_wall_ns
                    message.receipt_mono_ns = receipt_mono_ns
                    message.received_at_ns = receipt_mono_ns
                    message.parsed_mono_ns = parsed_mono_ns
                    message.event_id = (
                        f"m{next(_MARKET_EVENT_IDS)}"
                    )
                    stream = stream_name(message.topic)
                    root = tracer().start_span(
                        "market.event",
                        start_time=receipt_wall_ns,
                        attributes={
                            "market.event_id": message.event_id,
                            "market.topic": message.topic,
                            "market.stream": stream,
                            "market.exchange_ts_ms": int(
                                message.cts
                                or message.ts
                                or 0
                            ),
                        },
                    )
                    root_context = root.get_span_context()
                    if root_context.is_valid:
                        message.trace_id = (
                            f"{root_context.trace_id:032x}"
                        )
                    message.otel_span = root
                    root.add_event(
                        "received",
                        timestamp=receipt_wall_ns,
                    )
                    root.add_event(
                        "parsed",
                        timestamp=(
                            receipt_wall_ns
                            + parsed_mono_ns
                            - receipt_mono_ns
                        ),
                    )
                    receive_parse = max(
                        0.0,
                        (
                            parsed_mono_ns
                            - receipt_mono_ns
                        )
                        / 1_000_000_000,
                    )
                    exchange_receive = (
                        exchange_receive_seconds(message)
                    )
                    with trace.use_span(
                        root,
                        end_on_exit=False,
                    ):
                        observe_latency(
                            "receive_to_parse",
                            receive_parse,
                            stream=stream,
                        )
                        observe_latency(
                            "exchange_to_receive",
                            exchange_receive,
                            stream=stream,
                        )
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull:
                        try:
                            await asyncio.wait_for(
                                queue.put(message),
                                timeout=max(
                                    0.0,
                                    queue_put_timeout_seconds,
                                ),
                            )
                        except TimeoutError as exc:
                            if message.otel_span is not None:
                                message.otel_span.end()
                                message.otel_span = None
                            raise MarketDataBackpressureError(
                                "market ingest queue remained full "
                                f"(size={queue.maxsize})"
                            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception:
            if not stop_event.is_set():
                await asyncio.sleep(2)
        finally:
            if processor is not None:
                if not processor.done():
                    processor.cancel()
                await asyncio.gather(
                    processor,
                    return_exceptions=True,
                )
            if queue is not None:
                while True:
                    try:
                        queued = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        if queued.otel_span is not None:
                            queued.otel_span.end()
                            queued.otel_span = None
                    finally:
                        queue.task_done()


async def stream_symbol(
    ws_url: str,
    symbol: str,
    callback: StreamCallback,
    stop_event: asyncio.Event,
    orderbook_depth: int | None = None,
    *,
    fast_orderbook_depth: int = 50,
    deep_orderbook_depth: int = 1000,
    market_queue_size: int = 512,
    market_queue_put_timeout_seconds: float = 0.05,
    market_queue_max_lag_seconds: float = 0.50,
) -> None:
    valid_depths = {1, 50, 200, 1000}
    if orderbook_depth is not None:
        # Backwards-compatible single-book mode for external callers/tests.
        fast_orderbook_depth = orderbook_depth
        deep_orderbook_depth = orderbook_depth
    if fast_orderbook_depth not in valid_depths:
        raise ValueError(
            "Bybit fast orderbook depth must be one of 1, 50, 200, 1000"
        )
    if deep_orderbook_depth not in valid_depths:
        raise ValueError(
            "Bybit deep orderbook depth must be one of 1, 50, 200, 1000"
        )

    fast_topics = [
        f"orderbook.{fast_orderbook_depth}.{symbol}",
        f"kline.1.{symbol}",
        f"publicTrade.{symbol}",
    ]
    if deep_orderbook_depth == fast_orderbook_depth:
        await _stream_topics(
            ws_url,
            fast_topics,
            callback,
            stop_event,
            queue_size=market_queue_size,
            queue_put_timeout_seconds=(
                market_queue_put_timeout_seconds
            ),
            queue_max_lag_seconds=(
                market_queue_max_lag_seconds
            ),
        )
        return

    # Keep the latency-sensitive and deep-liquidity feeds on independent
    # connections. A reconnect/desync on one depth must not stop the other.
    await asyncio.gather(
        _stream_topics(
            ws_url,
            fast_topics,
            callback,
            stop_event,
            queue_size=market_queue_size,
            queue_put_timeout_seconds=(
                market_queue_put_timeout_seconds
            ),
            queue_max_lag_seconds=(
                market_queue_max_lag_seconds
            ),
        ),
        _stream_topics(
            ws_url,
            [f"orderbook.{deep_orderbook_depth}.{symbol}"],
            callback,
            stop_event,
            queue_size=market_queue_size,
            queue_put_timeout_seconds=(
                market_queue_put_timeout_seconds
            ),
            queue_max_lag_seconds=(
                market_queue_max_lag_seconds
            ),
        ),
    )
