from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import Counter, Gauge, Histogram

from .config import Settings


LATENCY_BUCKETS = (
    0.0005,
    0.001,
    0.002,
    0.005,
    0.010,
    0.020,
    0.050,
    0.100,
    0.200,
    0.500,
    1.000,
    2.000,
    5.000,
    10.000,
    15.000,
    30.000,
)

LATENCY_SECONDS = Histogram(
    "scalp_latency_seconds",
    "Latency distribution for the market-to-execution pipeline.",
    ["stage", "stream", "strategy", "execution_mode"],
    buckets=LATENCY_BUCKETS,
)

LATENCY_EVENTS = Counter(
    "scalp_latency_events_total",
    "Latency observations emitted by stage.",
    ["stage", "status"],
)

MARKET_QUEUE_LAG_SECONDS = Histogram(
    "scalp_market_queue_lag_seconds",
    "Time a decoded market message waited before processing.",
    ["stream"],
    buckets=LATENCY_BUCKETS,
)

MARKET_QUEUE_DEPTH = Gauge(
    "scalp_market_queue_depth",
    "Current decoded market queue depth.",
    ["stream", "symbol"],
)

RECORDER_PENDING_ROWS = Gauge(
    "scalp_recorder_pending_rows",
    "Recorder rows queued but not yet persisted.",
)

RECORDER_DROPPED_ROWS = Gauge(
    "scalp_recorder_dropped_rows",
    "Recorder rows dropped after a writer failure.",
)

_otel_provider: TracerProvider | None = None
_otel_configured = False


def _trace_endpoint(base: str) -> str:
    endpoint = str(base or "").strip().rstrip("/")
    if not endpoint:
        return ""
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return endpoint + "/v1/traces"


def configure_telemetry(config: Settings) -> None:
    global _otel_configured, _otel_provider
    if _otel_configured or not config.otel_enabled:
        return

    ratio = max(0.0, min(1.0, float(config.otel_trace_sample_ratio)))
    provider = TracerProvider(
        resource=Resource.create({
            SERVICE_NAME: config.otel_service_name,
            "service.version": "0.2.0",
        }),
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )

    endpoint = _trace_endpoint(config.otel_exporter_otlp_endpoint)
    if endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint)
            )
        )

    trace.set_tracer_provider(provider)
    _otel_provider = provider
    _otel_configured = True


def shutdown_telemetry() -> None:
    global _otel_provider
    provider = _otel_provider
    if provider is not None:
        provider.shutdown()
        _otel_provider = None


def tracer():
    return trace.get_tracer("scalp_bot.latency")


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


def _exemplar() -> dict[str, str] | None:
    trace_id = current_trace_id()
    if trace_id is None:
        return None
    return {"trace_id": trace_id}


def observe_latency(
    stage: str,
    seconds: float | None,
    *,
    stream: str = "",
    strategy: str = "",
    execution_mode: str = "",
    status: str = "ok",
) -> None:
    if seconds is None:
        return
    value = float(seconds)
    if value < 0:
        LATENCY_EVENTS.labels(stage, "negative").inc()
        return
    LATENCY_SECONDS.labels(
        stage,
        stream,
        strategy,
        execution_mode,
    ).observe(
        value,
        exemplar=_exemplar(),
    )
    LATENCY_EVENTS.labels(stage, status).inc()


def observe_market_queue(
    *,
    stream: str,
    symbol: str,
    depth: int,
    lag_seconds: float,
) -> None:
    MARKET_QUEUE_DEPTH.labels(stream, symbol).set(
        max(0, int(depth))
    )
    MARKET_QUEUE_LAG_SECONDS.labels(stream).observe(
        max(0.0, float(lag_seconds)),
        exemplar=_exemplar(),
    )


def observe_recorder_health(health: dict[str, Any]) -> None:
    RECORDER_PENDING_ROWS.set(
        max(0, int(health.get("pendingRows") or 0))
    )
    RECORDER_DROPPED_ROWS.set(
        max(0, int(health.get("droppedRows") or 0))
    )


def stream_name(topic: str | None) -> str:
    value = str(topic or "")
    if value.startswith("publicTrade."):
        return "public_trade"
    if value.startswith("kline."):
        return "kline_1m"
    if value.startswith("orderbook."):
        parts = value.split(".")
        depth = parts[1] if len(parts) > 1 else "unknown"
        return f"orderbook_{depth}"
    return "other"


def _mono_seconds(
    message: Any,
    start_attr: str,
    end_attr: str,
) -> float | None:
    start = int(getattr(message, start_attr, 0) or 0)
    end = int(getattr(message, end_attr, 0) or 0)
    if start <= 0 or end <= 0:
        return None
    return max(0.0, (end - start) / 1_000_000_000)


def exchange_receive_seconds(message: Any) -> float | None:
    exchange_ms = int(
        getattr(message, "cts", 0)
        or getattr(message, "ts", 0)
        or 0
    )
    receipt_wall_ns = int(
        getattr(message, "receipt_wall_ns", 0) or 0
    )
    if exchange_ms <= 0 or receipt_wall_ns <= 0:
        return None
    return max(
        0.0,
        receipt_wall_ns / 1_000_000_000
        - exchange_ms / 1000,
    )


def stage_wall_ns(message: Any, mono_attr: str) -> int | None:
    receipt_wall_ns = int(
        getattr(message, "receipt_wall_ns", 0) or 0
    )
    receipt_mono_ns = int(
        getattr(message, "receipt_mono_ns", 0) or 0
    )
    stage_mono_ns = int(
        getattr(message, mono_attr, 0) or 0
    )
    if (
        receipt_wall_ns <= 0
        or receipt_mono_ns <= 0
        or stage_mono_ns <= 0
    ):
        return None
    return receipt_wall_ns + (
        stage_mono_ns - receipt_mono_ns
    )


def latency_snapshot(message: Any | None) -> dict | None:
    if message is None:
        return None
    stages = {
        "receiptTsNs": int(
            getattr(message, "receipt_wall_ns", 0) or 0
        ) or None,
        "parsedTsNs": stage_wall_ns(
            message,
            "parsed_mono_ns",
        ),
        "processorTsNs": stage_wall_ns(
            message,
            "processor_started_mono_ns",
        ),
        "bookUpdatedTsNs": stage_wall_ns(
            message,
            "book_updated_mono_ns",
        ),
        "featuresReadyTsNs": stage_wall_ns(
            message,
            "features_ready_mono_ns",
        ),
        "strategyEvalTsNs": stage_wall_ns(
            message,
            "strategy_eval_started_mono_ns",
        ),
        "fireTsNs": stage_wall_ns(
            message,
            "fire_mono_ns",
        ),
        "orderSentTsNs": stage_wall_ns(
            message,
            "order_sent_mono_ns",
        ),
        "orderAckTsNs": stage_wall_ns(
            message,
            "order_ack_mono_ns",
        ),
        "fillTsNs": stage_wall_ns(
            message,
            "fill_mono_ns",
        ),
    }
    return {
        "eventId": getattr(message, "event_id", None),
        "traceId": (
            getattr(message, "trace_id", None)
            or current_trace_id()
        ),
        "topic": getattr(message, "topic", None),
        "exchangeTsMs": int(
            getattr(message, "cts", 0)
            or getattr(message, "ts", 0)
            or 0
        ) or None,
        **stages,
        "durationsMs": {
            "exchangeToReceive": (
                exchange_receive_seconds(message) * 1000
                if exchange_receive_seconds(message)
                is not None
                else None
            ),
            "receiveToParse": (
                _mono_seconds(
                    message,
                    "receipt_mono_ns",
                    "parsed_mono_ns",
                ) or 0.0
            ) * 1000
            if int(getattr(message, "parsed_mono_ns", 0) or 0)
            else None,
            "parseToStrategy": (
                _mono_seconds(
                    message,
                    "parsed_mono_ns",
                    "strategy_eval_started_mono_ns",
                ) or 0.0
            ) * 1000
            if int(
                getattr(
                    message,
                    "strategy_eval_started_mono_ns",
                    0,
                )
                or 0
            )
            else None,
            "strategyToFire": (
                _mono_seconds(
                    message,
                    "strategy_eval_started_mono_ns",
                    "fire_mono_ns",
                ) or 0.0
            ) * 1000
            if int(getattr(message, "fire_mono_ns", 0) or 0)
            else None,
            "fireToOrder": (
                _mono_seconds(
                    message,
                    "fire_mono_ns",
                    "order_sent_mono_ns",
                ) or 0.0
            ) * 1000
            if int(
                getattr(message, "order_sent_mono_ns", 0)
                or 0
            )
            else None,
            "orderToFill": (
                _mono_seconds(
                    message,
                    "order_sent_mono_ns",
                    "fill_mono_ns",
                ) or 0.0
            ) * 1000
            if int(getattr(message, "fill_mono_ns", 0) or 0)
            else None,
        },
    }


@contextmanager
def span(name: str, **attributes: Any):
    clean = {
        key: value
        for key, value in attributes.items()
        if value is not None
    }
    with tracer().start_as_current_span(
        name,
        attributes=clean,
    ) as active:
        yield active
