from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import msgspec
import zstandard as zstd


_DECODER = msgspec.json.Decoder(type=dict)
_CHUNK_SIZE = 4 * 1024 * 1024


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    return 0.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _existing_archive_dir(
    target_dir: Path,
    archive_name: str,
) -> bool:
    return all(
        path.is_file()
        for path in (
            target_dir / archive_name,
            target_dir / f"{archive_name}.sha256",
            target_dir / "metadata.json",
        )
    )


def _copy_verified_archive(
    source_dir: Path,
    mirror_root: Path,
    *,
    archive_name: str,
    archive_sha256: str,
    overwrite: bool,
) -> Path:
    target_dir = mirror_root / source_dir.name
    if target_dir.exists():
        if not overwrite:
            if _existing_archive_dir(
                target_dir,
                archive_name,
            ):
                existing_sha = _sha256_file(
                    target_dir / archive_name
                )
                if existing_sha == archive_sha256:
                    return target_dir
            raise FileExistsError(
                f"Raw archive mirror already exists: {target_dir}"
            )
        shutil.rmtree(target_dir)

    target_dir.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    shutil.copytree(source_dir, target_dir)
    copied_sha = _sha256_file(
        target_dir / archive_name
    )
    if copied_sha != archive_sha256:
        shutil.rmtree(target_dir)
        raise IOError(
            "Raw archive mirror checksum mismatch after copy"
        )
    return target_dir


def build_raw_session_archive(
    session_path: str | Path,
    *,
    output_root: str | Path = "data/raw-archives",
    mirror_root: str | Path | None = None,
    compression_level: int = 10,
    bot_commit: str | None = None,
    run_profile: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Preserve a lossless raw JSONL session as an immutable zstd archive."""
    source = Path(session_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if compression_level < 1 or compression_level > 22:
        raise ValueError(
            "compression_level must be between 1 and 22"
        )

    target_dir = Path(output_root) / source.stem
    archive_name = f"{source.name}.zst"
    archive_path = target_dir / archive_name

    if target_dir.exists():
        if not overwrite:
            if _existing_archive_dir(
                target_dir,
                archive_name,
            ):
                if mirror_root is not None:
                    metadata = json.loads(
                        (target_dir / "metadata.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    archive_sha = str(
                        (metadata.get("archive") or {}).get(
                            "sha256"
                        )
                        or _sha256_file(archive_path)
                    )
                    _copy_verified_archive(
                        target_dir,
                        Path(mirror_root),
                        archive_name=archive_name,
                        archive_sha256=archive_sha,
                        overwrite=False,
                    )
                return target_dir
            raise FileExistsError(
                f"Incomplete raw archive exists: {target_dir}"
            )
        shutil.rmtree(target_dir)

    target_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    partial_path = target_dir / f"{archive_name}.partial"

    raw_digest = hashlib.sha256()
    event_counts: Counter[str] = Counter()
    symbols: set[str] = set()
    row_count = 0
    first_ts: float | None = None
    last_ts: float | None = None
    run_summary: dict | None = None
    run_label: str | None = None

    compressor = zstd.ZstdCompressor(
        level=compression_level,
    )
    try:
        with (
            source.open("rb") as src,
            partial_path.open("wb") as raw_out,
            compressor.stream_writer(
                raw_out,
                closefd=False,
            ) as compressed,
        ):
            for line in src:
                raw_digest.update(line)
                compressed.write(line)

                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = _DECODER.decode(stripped)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue

                row_count += 1
                event = str(row.get("event") or "")
                event_counts[event] += 1
                symbol = str(row.get("symbol") or "")
                if symbol:
                    symbols.add(symbol)

                ts = _row_ts(row)
                if ts > 0:
                    first_ts = (
                        ts
                        if first_ts is None
                        else min(first_ts, ts)
                    )
                    last_ts = (
                        ts
                        if last_ts is None
                        else max(last_ts, ts)
                    )

                payload = row.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if event == "run_summary":
                    run_summary = dict(payload)
                elif event == "bot_started":
                    config = payload.get("config")
                    if isinstance(config, dict):
                        raw_label = (
                            config.get("runLabel")
                            or config.get("run_label")
                        )
                        if raw_label:
                            run_label = str(raw_label)

        partial_path.replace(archive_path)

        raw_sha256 = raw_digest.hexdigest()
        archive_sha256 = _sha256_file(
            archive_path
        )
        checksum_path = (
            target_dir
            / f"{archive_name}.sha256"
        )
        checksum_path.write_text(
            f"{archive_sha256}  {archive_name}\n",
            encoding="utf-8",
        )

        first = first_ts or 0.0
        last = last_ts if last_ts is not None else first
        metadata = {
            "schemaVersion": 1,
            "format": "scalp-bot-raw-session-archive",
            "generatedAt": datetime.now(UTC).isoformat(),
            "sessionId": source.stem,
            "source": {
                "file": source.name,
                "sizeBytes": source.stat().st_size,
                "rows": row_count,
                "eventCounts": dict(
                    sorted(event_counts.items())
                ),
                "symbols": sorted(symbols),
                "firstTs": first,
                "lastTs": last,
                "durationSeconds": max(
                    0.0,
                    last - first,
                ),
                "sha256": raw_sha256,
            },
            "archive": {
                "file": archive_name,
                "sizeBytes": archive_path.stat().st_size,
                "sha256": archive_sha256,
                "checksumFile": checksum_path.name,
                "compression": "zstd",
                "compressionLevel": compression_level,
            },
            "provenance": {
                "botCommit": bot_commit,
                "runProfile": run_profile,
                "runLabel": run_label,
            },
            "runSummary": run_summary,
            "integrity": {
                "sourcePreservedUnmodified": True,
                "archiveChecksumCovers": archive_name,
                "rawChecksumStoredInMetadata": True,
            },
        }
        (target_dir / "metadata.json").write_text(
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if mirror_root is not None:
            _copy_verified_archive(
                target_dir,
                Path(mirror_root),
                archive_name=archive_name,
                archive_sha256=archive_sha256,
                overwrite=overwrite,
            )
    except BaseException:
        partial_path.unlink(
            missing_ok=True,
        )
        if target_dir.exists() and not any(
            target_dir.iterdir()
        ):
            target_dir.rmdir()
        raise

    return target_dir
