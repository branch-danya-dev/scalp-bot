from __future__ import annotations

import io
from pathlib import Path

import pytest

from scalp_bot.git_session_export import (
    _RollingTradeDeltaNormalizer,
    _build_compact_session_summary,
    _build_navigation_index,
    _scan_session,
    _split_jsonl_stream,
)


def test_jsonl_stream_is_split_below_configured_limit(
    tmp_path: Path,
) -> None:
    rows = [
        b'{"n":1}\n',
        b'{"n":2}\n',
        b'{"n":3}\n',
    ]
    source = io.BytesIO(b"".join(rows))
    parts = _split_jsonl_stream(
        source,
        output_dir=tmp_path / "parts",
        root=tmp_path,
        max_file_bytes=16,
    )

    assert len(parts) == 2
    assert all(part["sizeBytes"] <= 16 for part in parts)
    assert sum(part["lines"] for part in parts) == 3
    assert parts[0]["path"] == "parts/part-0000.jsonl"
    assert parts[1]["path"] == "parts/part-0001.jsonl"


def test_jsonl_stream_rejects_single_row_larger_than_git_part(
    tmp_path: Path,
) -> None:
    source = io.BytesIO(b'{"payload":"' + b"x" * 100 + b'"}\n')

    with pytest.raises(
        ValueError,
        match="single JSONL row exceeds",
    ):
        _split_jsonl_stream(
            source,
            output_dir=tmp_path / "parts",
            root=tmp_path,
            max_file_bytes=32,
        )



def test_navigation_index_points_to_active_shards(
    tmp_path: Path,
) -> None:
    analysis = (
        tmp_path
        / "shards"
        / "0000-0030"
        / "analysis"
        / "part-0000.jsonl"
    )
    analysis.parent.mkdir(parents=True)
    rows = [
        {
            "ts": 1.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "level_breakout",
                "action": "long",
                "setupId": "break:1",
            },
        },
        {
            "ts": 2.0,
            "event": "trade_opened",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "level_breakout",
                "setupId": "break:1",
            },
        },
        {
            "ts": 3.0,
            "event": "risk_reject",
            "symbol": "BBBUSDT",
            "payload": {
                "strategy": "weak_level_rejection",
                "reason": "test gate",
            },
        },
        {
            "ts": 4.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {},
        },
    ]
    analysis.write_text(
        "".join(
            __import__("json").dumps(row) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    shard_rows = [
        {
            "id": "0000-0030",
            "index": 0,
            "analysisParts": [
                {
                    "path": analysis.relative_to(
                        tmp_path
                    ).as_posix(),
                }
            ],
            "orderbookParts": [],
        }
    ]

    navigation = _build_navigation_index(
        tmp_path,
        shard_rows,
        max_file_bytes=1024,
    )

    shard = shard_rows[0]
    assert shard["eventCounts"]["trade_opened"] == 1
    assert shard["activity"]["tradesOpened"] == 1
    assert shard["activity"]["riskRejects"] == 1
    assert shard["symbols"] == ["AAAUSDT", "BBBUSDT"]
    assert navigation["tradeEventParts"]
    assert navigation["problemEventParts"]
    assert (tmp_path / navigation["shards"]).is_file()



def test_rolling_trade_tape_is_exported_as_delta() -> None:
    normalizer = _RollingTradeDeltaNormalizer()

    first = normalizer.transform(
        {
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "tradeEncoding": "rolling_v1",
                "recentTrades": [
                    {
                        "ts": 1000,
                        "price": 100.0,
                        "size": 1.0,
                        "side": "Buy",
                        "sequence": 10,
                    },
                    {
                        "ts": 1001,
                        "price": 100.1,
                        "size": 1.0,
                        "side": "Sell",
                        "sequence": 11,
                    },
                ],
            },
        }
    )
    second = normalizer.transform(
        {
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "tradeEncoding": "rolling_v1",
                "recentTrades": [
                    {
                        "ts": 1000,
                        "price": 100.0,
                        "size": 1.0,
                        "side": "Buy",
                        "sequence": 10,
                    },
                    {
                        "ts": 1001,
                        "price": 100.1,
                        "size": 1.0,
                        "side": "Sell",
                        "sequence": 11,
                    },
                    {
                        "ts": 1002,
                        "price": 100.2,
                        "size": 1.0,
                        "side": "Buy",
                        "sequence": 12,
                    },
                ],
            },
        }
    )

    assert len(first["payload"]["recentTrades"]) == 2
    assert [
        trade["sequence"]
        for trade in second["payload"]["recentTrades"]
    ] == [12]
    assert second["payload"]["tradeEncoding"] == (
        "delta_v1_exported_from_rolling"
    )
    assert second["payload"]["tradeDeltaFromSequence"] == 11



def test_compact_session_summary_has_no_unbounded_report_payloads() -> None:
    summary = _build_compact_session_summary(
        bundle_manifest={
            "source": {
                "file": "session-test.jsonl",
                "durationSeconds": 3600,
            }
        },
        shard_rows=[
            {
                "id": "0000-0030",
                "coreStartTs": 1.0,
                "coreEndTs": 1801.0,
                "eventCounts": {
                    "decision": 10,
                    "trade_opened": 2,
                },
                "activity": {
                    "signals": 10,
                    "tradesOpened": 2,
                },
                "symbols": ["AAAUSDT"],
            },
            {
                "id": "0030-0060",
                "coreStartTs": 1801.0,
                "coreEndTs": 3601.0,
                "eventCounts": {
                    "decision": 8,
                    "risk_reject": 3,
                },
                "activity": {
                    "signals": 8,
                    "riskRejects": 3,
                },
                "symbols": ["AAAUSDT", "BBBUSDT"],
            },
        ],
        run_summary={"netPnl": 12.5},
    )

    assert summary["eventCounts"]["decision"] == 18
    assert summary["activity"]["signals"] == 18
    assert summary["activity"]["tradesOpened"] == 2
    assert summary["activity"]["riskRejects"] == 3
    assert summary["symbols"] == ["AAAUSDT", "BBBUSDT"]
    assert summary["runSummary"]["netPnl"] == 12.5
    assert "postRunOpportunity" not in summary
    assert "tradeReviews" not in summary
    assert "marketData" not in summary



def test_scan_session_is_lightweight_and_finds_focus(
    tmp_path: Path,
) -> None:
    source = tmp_path / "session-test.jsonl"
    rows = [
        {
            "ts": 100.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "level_breakout",
                "details": {"state": "armed"},
            },
        },
        {
            "ts": 101.0,
            "event": "risk_reject",
            "symbol": "AAAUSDT",
            "payload": {"strategy": "level_breakout"},
        },
        {
            "ts": 102.0,
            "event": "run_summary",
            "symbol": None,
            "payload": {"netPnl": 1.5},
        },
    ]
    source.write_text(
        "".join(
            __import__("json").dumps(row) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )

    (
        counts,
        symbols,
        focus,
        first_ts,
        last_ts,
        row_count,
        run_summary,
    ) = _scan_session(source)

    assert row_count == 3
    assert counts["decision"] == 1
    assert counts["risk_reject"] == 1
    assert symbols == {"AAAUSDT"}
    assert first_ts == 100.0
    assert last_ts == 102.0
    assert focus["AAAUSDT"]
    assert run_summary == {"netPnl": 1.5}
