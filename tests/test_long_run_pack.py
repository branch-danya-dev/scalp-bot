import json
import zipfile

from scalp_bot.long_run_pack import (
    build_long_run_analysis_bundle,
)


def write_row(fh, ts, event, symbol, payload):
    fh.write(json.dumps({
        "ts": ts,
        "iso": "2026-09-24T00:00:00+00:00",
        "event": event,
        "symbol": symbol,
        "payload": payload,
    }) + "\n")


def frame_payload(ts, sequence):
    return {
        "lastPrice": 100.0,
        "trend": "flat",
        "marketContext": {
            "legacyTrend": "flat",
            "htfBias": {"bias": "neutral"},
            "localRegime": {"regime": "range"},
            "flowContext": {
                "dominantDirection": "neutral",
            },
            "liquidityEvidence": {
                "state": "search",
            },
            "structureContext": None,
            "executionContext": {
                "ready": True,
            },
        },
        "analysisRuntime": {
            "mode": "live_fast_path",
        },
        "candle": {
            "time": int(ts),
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "volume": 10,
            "turnover": 1000,
            "confirmed": True,
        },
        "fastOrderbook": {
            "bids": [[99.99, 10, 999.9]],
            "asks": [[100.01, 10, 1000.1]],
            "bestBid": 99.99,
            "bestAsk": 100.01,
            "spreadPct": 0.0002,
        },
        "deepOrderbook": {
            "bids": [
                [99.99 - i * 0.01, 10, 999.9]
                for i in range(30)
            ],
            "asks": [
                [100.01 + i * 0.01, 10, 1000.1]
                for i in range(30)
            ],
            "bestBid": 99.99,
            "bestAsk": 100.01,
            "spreadPct": 0.0002,
        },
        "tradeFlow": {
            "imbalance5s": 0.0,
            "imbalance15s": 0.0,
            "imbalance60s": 0.0,
        },
        "bookFlow": {},
        "tradeEncoding": "delta_v1",
        "tradeCursor": sequence,
        "tradeDeltaFromSequence": sequence - 1,
        "tradeDeltaGap": False,
        "recentTrades": [{
            "ts": int(ts * 1000),
            "price": 100.0,
            "size": 1.0,
            "side": "Buy",
            "notional": 100.0,
            "sequence": sequence,
        }],
        "structure": {
            "levels": [],
            "trendlines": [],
            "dayHigh": None,
            "dayLow": None,
            "previousDayHigh": None,
            "previousDayLow": None,
        },
    }


def test_long_run_bundle_creates_overview_and_hourly_shards(
    tmp_path,
) -> None:
    source = tmp_path / "session-long.jsonl"
    with source.open("w", encoding="utf-8") as fh:
        write_row(
            fh,
            1.0,
            "symbol_activated",
            "AAAUSDT",
            {
                "market": {
                    "candles": [
                        {
                            "time": i * 60,
                            "open": 100.0,
                            "high": 100.1,
                            "low": 99.9,
                            "close": 100.0,
                            "volume": 10,
                            "turnover": 1000,
                            "confirmed": True,
                        }
                        for i in range(80)
                    ],
                    "chartSeries": {"1m": []},
                }
            },
        )
        write_row(
            fh,
            10.0,
            "research_frame",
            "AAAUSDT",
            frame_payload(10.0, 1),
        )
        write_row(
            fh,
            11.0,
            "decision",
            "AAAUSDT",
            {
                "strategy": "level_breakout",
                "action": "wait",
                "reasons": ["armed"],
                "details": {
                    "state": "armed",
                },
            },
        )
        write_row(
            fh,
            3700.0,
            "research_frame",
            "AAAUSDT",
            frame_payload(3700.0, 2),
        )
        write_row(
            fh,
            3701.0,
            "run_summary",
            None,
            {
                "runLabel": "test-10h",
                "closedTrades": 0,
                "realizedPnl": 0.0,
            },
        )

    bundle = build_long_run_analysis_bundle(
        source,
        output_dir=tmp_path / "bundle",
        shard_seconds=3600,
        overview_frame_seconds=10,
        shard_frame_seconds=3,
    )

    overview = bundle / "session-long-overview.zip"
    shard0 = bundle / "session-long-hour-00.zip"
    shard1 = bundle / "session-long-hour-01.zip"

    assert overview.is_file()
    assert shard0.is_file()
    assert shard1.is_file()

    manifest = json.loads(
        (bundle / "bundle-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["sharding"]["shardCount"] == 2
    assert len(manifest["shards"]) == 2

    with zipfile.ZipFile(overview) as archive:
        assert {
            "manifest.json",
            "README.txt",
            "session-report.json",
            "critical-events.jsonl",
            "latency-summary.json",
            "shard-index.json",
            "overview-analysis.jsonl",
        } <= set(archive.namelist())

    with zipfile.ZipFile(shard1) as archive:
        analysis = archive.read(
            "session-analysis.jsonl"
        ).decode("utf-8")
        focus = archive.read(
            "focus-orderbooks.jsonl"
        ).decode("utf-8")

    assert '"tradeEncoding":"delta_v1"' in analysis
    assert '"sequence":2' in analysis
    assert '"deepOrderbook"' in analysis
    assert focus
