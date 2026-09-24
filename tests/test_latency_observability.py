from __future__ import annotations

from prometheus_client import REGISTRY

from scalp_bot.bybit import MarketMessage
from scalp_bot.latency_observability import (
    latency_snapshot,
    observe_latency,
    stage_wall_ns,
    stream_name,
)


def _sample_value(
    name: str,
    labels: dict[str, str],
) -> float:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name != name:
                continue
            if all(
                sample.labels.get(key) == value
                for key, value in labels.items()
            ):
                return float(sample.value)
    return 0.0


def test_latency_snapshot_reconstructs_wall_clock_stages() -> None:
    message = MarketMessage(
        topic="orderbook.50.BTCUSDT",
        ts=1_700_000_000_000,
        event_id="m-test",
        trace_id="0" * 31 + "1",
        receipt_wall_ns=2_000_000_000,
        receipt_mono_ns=1_000_000_000,
        parsed_mono_ns=1_001_000_000,
        processor_started_mono_ns=1_003_000_000,
        book_updated_mono_ns=1_005_000_000,
        features_ready_mono_ns=1_010_000_000,
        strategy_eval_started_mono_ns=1_012_000_000,
        fire_mono_ns=1_020_000_000,
        order_sent_mono_ns=1_023_000_000,
        order_ack_mono_ns=1_024_000_000,
        fill_mono_ns=1_030_000_000,
    )

    snapshot = latency_snapshot(message)

    assert snapshot is not None
    assert snapshot["eventId"] == "m-test"
    assert snapshot["traceId"] == "0" * 31 + "1"
    assert snapshot["parsedTsNs"] == 2_001_000_000
    assert snapshot["fireTsNs"] == 2_020_000_000
    assert snapshot["durationsMs"]["receiveToParse"] == 1.0
    assert snapshot["durationsMs"]["parseToStrategy"] == 11.0
    assert snapshot["durationsMs"]["strategyToFire"] == 8.0
    assert snapshot["durationsMs"]["fireToOrder"] == 3.0
    assert snapshot["durationsMs"]["orderToFill"] == 7.0
    assert stage_wall_ns(
        message,
        "features_ready_mono_ns",
    ) == 2_010_000_000


def test_prometheus_latency_histogram_records_observation() -> None:
    labels = {
        "stage": "unit_test_stage",
        "stream": "public_trade",
        "strategy": "level_breakout",
        "execution_mode": "",
    }
    before = _sample_value(
        "scalp_latency_seconds_count",
        labels,
    )

    observe_latency(
        "unit_test_stage",
        0.012,
        stream="public_trade",
        strategy="level_breakout",
    )

    after = _sample_value(
        "scalp_latency_seconds_count",
        labels,
    )
    assert after == before + 1


def test_stream_name_has_bounded_topic_categories() -> None:
    assert stream_name(
        "orderbook.50.BTCUSDT"
    ) == "orderbook_50"
    assert stream_name(
        "orderbook.1000.BTCUSDT"
    ) == "orderbook_1000"
    assert stream_name(
        "publicTrade.BTCUSDT"
    ) == "public_trade"
    assert stream_name("kline.1.BTCUSDT") == "kline_1m"
