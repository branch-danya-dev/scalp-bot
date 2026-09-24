from __future__ import annotations

import io
from pathlib import Path

import pytest

from scalp_bot.git_session_export import _split_jsonl_stream


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
