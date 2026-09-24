from __future__ import annotations

import hashlib
import json
from pathlib import Path

import zstandard as zstd

from scalp_bot.raw_session_archive import (
    build_raw_session_archive,
)


def test_raw_archive_round_trip_and_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "session-test.jsonl"
    payload = (
        '{"ts":100.0,"event":"bot_started","symbol":null,'
        '"payload":{"config":{"runLabel":"unit-run"}}}\n'
        '{"ts":101.0,"event":"trade_closed","symbol":"AAAUSDT",'
        '"payload":{"netPnl":1.25}}\n'
        '{"ts":102.0,"event":"run_summary","symbol":null,'
        '"payload":{"netPnl":1.25}}\n'
    ).encode("utf-8")
    source.write_bytes(payload)

    archive_dir = build_raw_session_archive(
        source,
        output_root=tmp_path / "archives",
        bot_commit="abc123",
        run_profile=".env.test",
    )

    archive = archive_dir / "session-test.jsonl.zst"
    metadata = json.loads(
        (archive_dir / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    decompressed = zstd.ZstdDecompressor().decompress(
        archive.read_bytes()
    )

    assert decompressed == payload
    assert metadata["source"]["sha256"] == hashlib.sha256(
        payload
    ).hexdigest()
    assert metadata["archive"]["sha256"] == hashlib.sha256(
        archive.read_bytes()
    ).hexdigest()
    assert metadata["source"]["rows"] == 3
    assert metadata["source"]["symbols"] == ["AAAUSDT"]
    assert metadata["provenance"]["botCommit"] == "abc123"
    assert metadata["provenance"]["runLabel"] == "unit-run"
    assert metadata["runSummary"]["netPnl"] == 1.25
    checksum = (
        archive_dir
        / "session-test.jsonl.zst.sha256"
    ).read_text(encoding="utf-8")
    assert metadata["archive"]["sha256"] in checksum


def test_raw_archive_can_be_mirrored_and_reused(
    tmp_path: Path,
) -> None:
    source = tmp_path / "session-reuse.jsonl"
    source.write_text(
        '{"ts":1,"event":"run_summary","payload":{}}\n',
        encoding="utf-8",
    )
    primary = tmp_path / "primary"
    mirror = tmp_path / "mirror"

    first = build_raw_session_archive(
        source,
        output_root=primary,
        mirror_root=mirror,
    )
    second = build_raw_session_archive(
        source,
        output_root=primary,
        mirror_root=mirror,
    )

    assert second == first
    assert (
        mirror
        / source.stem
        / f"{source.name}.zst"
    ).is_file()
