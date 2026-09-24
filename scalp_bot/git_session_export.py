from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .long_run_pack import build_long_run_analysis_bundle


DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _copy_member(
    archive: zipfile.ZipFile,
    member: str,
    target: Path,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member, "r") as source, target.open("wb") as out:
        shutil.copyfileobj(source, out)


def _part_metadata(
    *,
    path: Path,
    root: Path,
    size_bytes: int,
    line_count: int,
    digest: str,
) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sizeBytes": size_bytes,
        "lines": line_count,
        "sha256": digest,
    }


def _split_jsonl_stream(
    source: BinaryIO,
    *,
    output_dir: Path,
    root: Path,
    max_file_bytes: int,
) -> list[dict]:
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")

    output_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    out = None
    out_path: Path | None = None
    digest = hashlib.sha256()
    size_bytes = 0
    line_count = 0
    part_index = -1

    def close_part() -> None:
        nonlocal out, out_path, digest, size_bytes, line_count
        if out is None or out_path is None:
            return
        out.close()
        parts.append(
            _part_metadata(
                path=out_path,
                root=root,
                size_bytes=size_bytes,
                line_count=line_count,
                digest=digest.hexdigest(),
            )
        )
        out = None
        out_path = None
        digest = hashlib.sha256()
        size_bytes = 0
        line_count = 0

    try:
        for raw_line in source:
            if not raw_line:
                continue
            if not raw_line.endswith(b"\n"):
                raw_line += b"\n"
            if len(raw_line) > max_file_bytes:
                raise ValueError(
                    "single JSONL row exceeds configured Git part size "
                    f"({len(raw_line)} > {max_file_bytes} bytes)"
                )
            if (
                out is not None
                and size_bytes > 0
                and size_bytes + len(raw_line) > max_file_bytes
            ):
                close_part()

            if out is None:
                part_index += 1
                out_path = output_dir / f"part-{part_index:04d}.jsonl"
                out = out_path.open("wb")

            out.write(raw_line)
            digest.update(raw_line)
            size_bytes += len(raw_line)
            line_count += 1
    finally:
        close_part()

    return parts


def _extract_jsonl_member(
    archive: zipfile.ZipFile,
    member: str,
    *,
    output_dir: Path,
    root: Path,
    max_file_bytes: int,
) -> list[dict]:
    with archive.open(member, "r") as source:
        return _split_jsonl_stream(
            source,
            output_dir=output_dir,
            root=root,
            max_file_bytes=max_file_bytes,
        )


def _shard_id(index: int, shard_minutes: float) -> str:
    start_minute = int(round(index * shard_minutes))
    end_minute = int(round((index + 1) * shard_minutes))
    return f"{start_minute:04d}-{end_minute:04d}"


def build_git_session_export(
    session_path: str | Path,
    *,
    output_root: str | Path,
    shard_minutes: float = 30.0,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    overview_frame_seconds: float = 10.0,
    shard_frame_seconds: float = 3.0,
    book_depth: int = 16,
    focus_book_depth: int = 50,
    book_sample_seconds: float = 15.0,
    horizon_seconds: float = 120.0,
    overwrite: bool = False,
) -> Path:
    """Build a Git-friendly analysis tree from a raw research session.

    The raw session remains local. Only compact analysis-grade text is emitted.
    Every JSONL stream is split into bounded parts so no individual Git object
    grows with the total run duration.
    """
    source = Path(session_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if shard_minutes <= 0:
        raise ValueError("shard_minutes must be positive")

    output_root = Path(output_root)
    run_dir = output_root / source.stem
    if run_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"Git export already exists: {run_dir}"
            )
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="scalp-git-export-",
    ) as temp_name:
        temp_root = Path(temp_name)
        bundle_dir = build_long_run_analysis_bundle(
            source,
            output_dir=temp_root / "bundle",
            shard_seconds=shard_minutes * 60.0,
            overview_frame_seconds=overview_frame_seconds,
            shard_frame_seconds=shard_frame_seconds,
            shard_book_depth=book_depth,
            focus_book_depth=focus_book_depth,
            orderbook_sample_seconds=book_sample_seconds,
            opportunity_horizon_seconds=horizon_seconds,
        )

        bundle_manifest = json.loads(
            (bundle_dir / "bundle-manifest.json").read_text(
                encoding="utf-8"
            )
        )

        overview_name = str(
            bundle_manifest["overviewArchive"]
        )
        overview_zip = bundle_dir / overview_name
        overview_dir = run_dir / "overview"
        with zipfile.ZipFile(overview_zip, "r") as archive:
            for member, target_name in (
                ("manifest.json", "source-manifest.json"),
                ("session-report.json", "session-report.json"),
                ("latency-summary.json", "latency-summary.json"),
                ("shard-index.json", "source-shard-index.json"),
                ("README.txt", "README.txt"),
            ):
                _copy_member(
                    archive,
                    member,
                    overview_dir / target_name,
                )

            critical_parts = _extract_jsonl_member(
                archive,
                "critical-events.jsonl",
                output_dir=overview_dir / "critical-events",
                root=run_dir,
                max_file_bytes=max_file_bytes,
            )
            overview_parts = _extract_jsonl_member(
                archive,
                "overview-analysis.jsonl",
                output_dir=overview_dir / "frames",
                root=run_dir,
                max_file_bytes=max_file_bytes,
            )

        shard_rows: list[dict] = []
        for shard in bundle_manifest.get("shards") or []:
            index = int(shard.get("shard") or 0)
            shard_name = _shard_id(index, shard_minutes)
            shard_dir = run_dir / "shards" / shard_name
            archive_name = str(shard["archive"])
            archive_path = bundle_dir / archive_name

            with zipfile.ZipFile(archive_path, "r") as archive:
                _copy_member(
                    archive,
                    "manifest.json",
                    shard_dir / "manifest.json",
                )
                analysis_parts = _extract_jsonl_member(
                    archive,
                    "session-analysis.jsonl",
                    output_dir=shard_dir / "analysis",
                    root=run_dir,
                    max_file_bytes=max_file_bytes,
                )
                orderbook_parts = _extract_jsonl_member(
                    archive,
                    "focus-orderbooks.jsonl",
                    output_dir=shard_dir / "orderbooks",
                    root=run_dir,
                    max_file_bytes=max_file_bytes,
                )

            shard_rows.append(
                {
                    "id": shard_name,
                    "index": index,
                    "coreStartTs": shard.get("coreStartTs"),
                    "coreEndTs": shard.get("coreEndTs"),
                    "rows": shard.get("rows"),
                    "frameRows": shard.get("frameRows"),
                    "orderbookRows": shard.get("orderbookRows"),
                    "tradeDeltaGapRows": shard.get(
                        "tradeDeltaGapRows"
                    ),
                    "manifest": (
                        shard_dir / "manifest.json"
                    ).relative_to(run_dir).as_posix(),
                    "analysisParts": analysis_parts,
                    "orderbookParts": orderbook_parts,
                }
            )

    manifest = {
        "schemaVersion": 1,
        "format": "scalp-bot-git-session-export",
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": bundle_manifest.get("source"),
        "rawSessionPolicy": (
            "raw session JSONL stays local and is not committed"
        ),
        "limits": {
            "maxFileBytes": max_file_bytes,
            "shardMinutes": shard_minutes,
        },
        "overview": {
            "sessionReport": "overview/session-report.json",
            "latencySummary": "overview/latency-summary.json",
            "sourceManifest": "overview/source-manifest.json",
            "sourceShardIndex": (
                "overview/source-shard-index.json"
            ),
            "criticalEventParts": critical_parts,
            "frameParts": overview_parts,
        },
        "shards": shard_rows,
    }
    _write_json(run_dir / "manifest.json", manifest)

    (run_dir / "README.md").write_text(
        (
            "# Scalp bot session export\n\n"
            "This directory is an analysis-grade export. The lossless raw "
            "session remains local and is intentionally not committed.\n\n"
            "Start with manifest.json, overview/session-report.json, "
            "and overview/latency-summary.json. Open only the referenced "
            "time shards when deeper tick/order-book inspection is needed.\n"
        ),
        encoding="utf-8",
    )

    return run_dir
