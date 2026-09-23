import json
import zipfile

from scalp_bot.analysis_pack import build_analysis_pack


def write_row(fh, ts, event, symbol, payload):
    fh.write(json.dumps({
        "ts": ts,
        "iso": "2026-09-22T00:00:00+00:00",
        "event": event,
        "symbol": symbol,
        "payload": payload,
    }) + "\n")


def test_analysis_pack_keeps_events_candles_reports_and_sampled_books(tmp_path):
    source = tmp_path / "session-test.jsonl"
    with source.open("w", encoding="utf-8") as fh:
        write_row(fh, 0.0, "scanner_update", None, {
            "active": ["AAAUSDT"],
            "promotedFromTop": ["AAAUSDT"],
            "ranked": [{"symbol": "AAAUSDT", "activity_rank": 1}],
        })
        write_row(fh, 1.0, "symbol_activated", "AAAUSDT", {
            "market": {
                "candles": [{"time": 1, "open": 100, "high": 100, "low": 100, "close": 100}],
                "chartSeries": {"1m": []},
            },
        })
        write_row(fh, 2.0, "decision", "AAAUSDT", {
            "strategy": "level_breakout",
            "action": "long",
            "entry": 100.0,
            "stop": 99.0,
            "target": 101.0,
            "reasons": ["confirmed"],
            "details": {
                "state": "break",
                "zone": {
                    "kind": "resistance",
                    "low": 99.9,
                    "high": 100.1,
                    "touches": 5,
                },
            },
            "trace": {
                "state": "break",
                "trend": "up",
                "object": {
                    "type": "horizontal_zone",
                    "label": "resistance",
                    "low": 99.9,
                    "high": 100.1,
                },
            },
        })
        write_row(fh, 3.0, "risk_reject", "AAAUSDT", {
            "strategy": "level_breakout",
            "reason": "test",
            "decision": {
                "strategy": "level_breakout",
                "action": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 101.0,
            },
        })
        for second in range(4, 10):
            write_row(fh, float(second), "research_frame", "AAAUSDT", {
                "lastPrice": 100.0 + second / 10,
                "trend": "up",
                "candle": {
                    "time": second,
                    "open": 100.0,
                    "high": 101.2 if second >= 5 else 100.5,
                    "low": 99.8,
                    "close": 100.0 + second / 10,
                },
                "orderbook": {
                    "bids": [[100.0 - i / 100, 1, 100.0] for i in range(100)],
                    "asks": [[100.1 + i / 100, 1, 100.1] for i in range(100)],
                },
                "tradeFlow": {"imbalance5s": 0.5},
            })
        write_row(fh, 10.0, "run_summary", None, {
            "runLabel": "test",
            "closedTrades": 0,
            "realizedPnl": 0.0,
        })

    output = build_analysis_pack(
        source,
        orderbook_sample_seconds=5,
        orderbook_sample_depth=4,
        focus_orderbook_depth=8,
    )

    assert output.exists()
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "README.txt",
            "session-report.json",
            "session-analysis.jsonl",
            "orderbook-samples.jsonl",
        }
        manifest = json.loads(archive.read("manifest.json"))
        report = json.loads(archive.read("session-report.json"))
        compact = archive.read("session-analysis.jsonl").decode("utf-8")
        books = [
            json.loads(line)
            for line in archive.read("orderbook-samples.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]

    assert manifest["source"]["sizeBytes"] > output.stat().st_size
    assert report["postRunOpportunity"]["summary"]["rejectedCandidates"] == 1
    assert report["marketInteractionResearch"]["summary"]["checkpoints"] == 1
    assert manifest["compaction"]["interactionDecisionFocusStates"]["level_breakout"] == [
        "break",
        "impulse",
    ]
    assert '"risk_reject"' in compact
    assert books
    assert max(len(row["payload"]["orderbook"]["bids"]) for row in books) == 8
