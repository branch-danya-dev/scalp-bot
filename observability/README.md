# Latency observability

This local stack adds Prometheus metrics, Grafana dashboards, Tempo traces, and an OpenTelemetry Collector without changing trading logic.

## Start the observability stack

From the repository root:

```powershell
docker compose -f .\observability\docker-compose.yml up -d
```

Then start the bot with the observability profile:

```powershell
.\scripts\run.ps1 -Profile .env.observability
```

Endpoints:

- Bot UI: http://127.0.0.1:8000/
- Prometheus metrics: http://127.0.0.1:8000/metrics
- Prometheus UI: http://127.0.0.1:9090/
- Grafana: http://127.0.0.1:3000/
- Tempo API: http://127.0.0.1:3200/
- OTLP/HTTP collector: http://127.0.0.1:4318/

Grafana provisions the **Scalp Bot — Latency** dashboard automatically.

## Latency stages

Prometheus histogram `scalp_latency_seconds` records:

- `exchange_to_receive`
- `receive_to_parse`
- `parse_to_processor`
- `processor_to_book`
- `parse_to_features`
- `book_to_features`
- `parse_to_strategy`
- `strategy_function`
- `strategy_evaluation`
- `strategy_to_fire`
- `exchange_to_fire`
- `fire_to_order`
- `order_to_ack`
- `order_to_fill`

The histogram labels are deliberately bounded to `stage`, `stream`, `strategy`, and `execution_mode`. Event IDs and setup IDs are not Prometheus labels.

Concrete event correlation uses OpenTelemetry traces and the source market `eventId`. Prometheus exemplars attach the active trace ID where available.

## Paper execution semantics

The current engine is paper-only. Therefore:

- `paper_taker` order ack/fill latency measures local broker simulation and depth-aware paper fill.
- `paper_maker` order ack is local pending-order placement.
- `paper_maker` order fill measures time until a qualifying trade-through volume fill occurs.

These are not Bybit private-order REST/WebSocket RTT measurements. When live execution is added, the same stages can be bound to real order-sent, exchange-ack, and fill timestamps.
