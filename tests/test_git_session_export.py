from __future__ import annotations

import io
from pathlib import Path

import pytest

from scalp_bot.git_session_export import (
    _build_navigation_index,
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
