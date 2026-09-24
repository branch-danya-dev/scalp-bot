from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .long_run_pack import build_long_run_analysis_bundle


DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024

TRADE_INDEX_EVENTS = {
    "entry_pending",
    "entry_add_pending",
    "entry_cancelled",
    "trade_opened",
    "position_added",
    "partial_take",
    "funding_payment",
    "trade_closed",
}
PROBLEM_INDEX_EVENTS = {
    "risk_reject",
    "setup_blocked",
    "arbiter_blocked",
    "scanner_error",
    "context_error",
    "fast_path_error",
    "strategy_error",
    "research_policy_blocked",
}


class _JsonlPartWriter:
    def __init__(
        self,
        *,
        output_dir: Path,
        root: Path,
        max_file_bytes: int,
    ) -> None:
        self.output_dir = output_dir
        self.root = root
        self.max_file_bytes = max_file_bytes
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.parts: list[dict] = []
        self._out = None
        self._path: Path | None = None
        self._digest = hashlib.sha256()
        self._size_bytes = 0
        self._line_count = 0
        self._part_index = -1

    def _close_part(self) -> None:
        if self._out is None or self._path is None:
            return
        self._out.close()
        self.parts.append(
            _part_metadata(
                path=self._path,
                root=self.root,
                size_bytes=self._size_bytes,
                line_count=self._line_count,
                digest=self._digest.hexdigest(),
            )
        )
        self._out = None
        self._path = None
        self._digest = hashlib.sha256()
        self._size_bytes = 0
        self._line_count = 0

    def write(self, value: dict) -> None:
        raw = (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(raw) > self.max_file_bytes:
            raise ValueError(
                "single index row exceeds configured Git part size "
                f"({len(raw)} > {self.max_file_bytes} bytes)"
            )
        if (
            self._out is not None
            and self._size_bytes > 0
            and self._size_bytes + len(raw) > self.max_file_bytes
        ):
            self._close_part()
        if self._out is None:
            self._part_index += 1
            self._path = (
                self.output_dir
                / f"part-{self._part_index:04d}.jsonl"
            )
            self._out = self._path.open("wb")

        self._out.write(raw)
        self._digest.update(raw)
        self._size_bytes += len(raw)
        self._line_count += 1

    def close(self) -> list[dict]:
        self._close_part()
        return list(self.parts)


def _compact_index_event(
    row: dict,
    *,
    shard_id: str,
) -> dict:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        plan = {}
    compact = {
        "ts": row.get("ts"),
        "iso": row.get("iso"),
        "shard": shard_id,
        "event": row.get("event"),
        "symbol": row.get("symbol"),
        "strategy": (
            payload.get("strategy")
            or plan.get("strategy")
        ),
        "action": payload.get("action"),
        "side": (
            payload.get("side")
            or plan.get("side")
        ),
        "setupId": (
            payload.get("setupId")
            or payload.get("setup_id")
            or plan.get("setupId")
            or plan.get("setup_id")
        ),
        "reason": payload.get("reason"),
        "entry": payload.get("entry"),
        "exit": payload.get("exit"),
        "netPnl": payload.get("netPnl"),
    }
    return {
        key: value
        for key, value in compact.items()
        if value is not None
    }


def _is_trade_index_event(
    event: str,
    payload: dict,
) -> bool:
    if event in TRADE_INDEX_EVENTS:
        return True
    if event != "decision":
        return False
    action = str(payload.get("action") or "").lower()
    return action in {"long", "short"}


def _build_navigation_index(
    run_dir: Path,
    shard_rows: list[dict],
    *,
    max_file_bytes: int,
) -> dict:
    index_dir = run_dir / "index"
    trade_writer = _JsonlPartWriter(
        output_dir=index_dir / "trade-events",
        root=run_dir,
        max_file_bytes=max_file_bytes,
    )
    problem_writer = _JsonlPartWriter(
        output_dir=index_dir / "problem-events",
        root=run_dir,
        max_file_bytes=max_file_bytes,
    )

    for shard in shard_rows:
        counts: Counter[str] = Counter()
        symbols: set[str] = set()
        shard_id = str(shard["id"])

        for part in shard.get("analysisParts") or []:
            part_path = run_dir / str(part["path"])
            with part_path.open(
                "r",
                encoding="utf-8",
            ) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    event = str(row.get("event") or "")
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}
                    if event:
                        counts[event] += 1
                    symbol = str(row.get("symbol") or "")
                    if symbol:
                        symbols.add(symbol)

                    if _is_trade_index_event(event, payload):
                        trade_writer.write(
                            _compact_index_event(
                                row,
                                shard_id=shard_id,
                            )
                        )
                    if event in PROBLEM_INDEX_EVENTS:
                        problem_writer.write(
                            _compact_index_event(
                                row,
                                shard_id=shard_id,
                            )
                        )

        shard["eventCounts"] = dict(
            sorted(counts.items())
        )
        shard["symbols"] = sorted(symbols)
        shard["activity"] = {
            "signals": counts.get("decision", 0),
            "entriesPending": (
                counts.get("entry_pending", 0)
                + counts.get("entry_add_pending", 0)
            ),
            "tradesOpened": counts.get("trade_opened", 0),
            "tradesClosed": counts.get("trade_closed", 0),
            "partialTakes": counts.get("partial_take", 0),
            "riskRejects": counts.get("risk_reject", 0),
            "setupBlocks": counts.get("setup_blocked", 0),
            "arbiterBlocks": counts.get("arbiter_blocked", 0),
            "entryCancels": counts.get("entry_cancelled", 0),
            "errors": (
                counts.get("scanner_error", 0)
                + counts.get("context_error", 0)
                + counts.get("fast_path_error", 0)
                + counts.get("strategy_error", 0)
            ),
        }

    trade_parts = trade_writer.close()
    problem_parts = problem_writer.close()
    shard_index = {
        "schemaVersion": 1,
        "shards": shard_rows,
    }
    _write_json(
        index_dir / "shards.json",
        shard_index,
    )
    return {
        "shards": "index/shards.json",
        "tradeEventParts": trade_parts,
        "problemEventParts": problem_parts,
    }


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


class _RollingTradeDeltaNormalizer:
    def __init__(self) -> None:
        self._last_sequence: dict[str, int] = {}
        self._seen_fallback: dict[str, set[tuple]] = {}

    @staticmethod
    def _trade_key(trade: dict) -> tuple:
        return (
            trade.get("ts"),
            trade.get("price"),
            trade.get("size"),
            trade.get("side"),
        )

    def transform(self, row: dict) -> dict:
        if row.get("event") != "research_frame":
            return row
        symbol = str(row.get("symbol") or "")
        payload = row.get("payload")
        if not symbol or not isinstance(payload, dict):
            return row

        encoding = str(
            payload.get("tradeEncoding") or "rolling_v1"
        )
        trades = payload.get("recentTrades")
        if (
            encoding.startswith("delta_v1")
            or not isinstance(trades, list)
        ):
            return row

        last_sequence = self._last_sequence.get(symbol, 0)
        max_sequence = last_sequence
        fallback_seen = self._seen_fallback.setdefault(
            symbol,
            set(),
        )
        selected: list[dict] = []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            raw_sequence = trade.get("sequence")
            try:
                sequence = int(raw_sequence or 0)
            except (TypeError, ValueError):
                sequence = 0

            if sequence > 0:
                max_sequence = max(max_sequence, sequence)
                if sequence <= last_sequence:
                    continue
                selected.append(trade)
                continue

            key = self._trade_key(trade)
            if key in fallback_seen:
                continue
            fallback_seen.add(key)
            selected.append(trade)

        if max_sequence > 0:
            self._last_sequence[symbol] = max_sequence
        if len(fallback_seen) > 5000:
            # Sequence-less trades are a legacy fallback. Bound memory while
            # retaining enough recent identity to remove rolling duplication.
            fallback_seen.clear()
            for trade in trades[-1000:]:
                if isinstance(trade, dict):
                    fallback_seen.add(
                        self._trade_key(trade)
                    )

        new_payload = dict(payload)
        new_payload["recentTrades"] = selected
        new_payload["tradeEncoding"] = (
            "delta_v1_exported_from_rolling"
        )
        new_payload["tradeDeltaFromSequence"] = (
            last_sequence if last_sequence > 0 else None
        )
        new_row = dict(row)
        new_row["payload"] = new_payload
        return new_row


def _extract_analysis_member(
    archive: zipfile.ZipFile,
    member: str,
    *,
    output_dir: Path,
    root: Path,
    max_file_bytes: int,
) -> list[dict]:
    normalizer = _RollingTradeDeltaNormalizer()
    writer = _JsonlPartWriter(
        output_dir=output_dir,
        root=root,
        max_file_bytes=max_file_bytes,
    )
    with archive.open(member, "r") as source:
        for raw_line in source:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            writer.write(
                normalizer.transform(row)
            )
    return writer.close()


def _shard_id(index: int, shard_minutes: float) -> str:
    start_minute = int(round(index * shard_minutes))
    end_minute = int(round((index + 1) * shard_minutes))
    return f"{start_minute:04d}-{end_minute:04d}"


def _find_run_summary(
    run_dir: Path,
    critical_parts: list[dict],
) -> dict | None:
    result = None
    for part in critical_parts:
        path = run_dir / str(part["path"])
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("event") == "run_summary"
                    and isinstance(row.get("payload"), dict)
                ):
                    result = dict(row["payload"])
    return result


def _build_compact_session_summary(
    *,
    bundle_manifest: dict,
    shard_rows: list[dict],
    run_summary: dict | None,
) -> dict:
    event_counts: Counter[str] = Counter()
    symbols: set[str] = set()
    activity: Counter[str] = Counter()

    for shard in shard_rows:
        event_counts.update(shard.get("eventCounts") or {})
        symbols.update(shard.get("symbols") or [])
        for key, value in (shard.get("activity") or {}).items():
            if isinstance(value, int):
                activity[key] += value

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": bundle_manifest.get("source"),
        "runSummary": run_summary,
        "eventCounts": dict(sorted(event_counts.items())),
        "activity": dict(sorted(activity.items())),
        "symbols": sorted(symbols),
        "shards": [
            {
                "id": shard.get("id"),
                "coreStartTs": shard.get("coreStartTs"),
                "coreEndTs": shard.get("coreEndTs"),
                "activity": shard.get("activity"),
                "symbols": shard.get("symbols"),
            }
            for shard in shard_rows
        ],
        "detailPolicy": (
            "Detailed opportunity/review/market data is stored in bounded "
            "indexes and time shards; no unbounded monolithic report is "
            "published to Git."
        ),
    }


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
                analysis_parts = _extract_analysis_member(
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

    navigation_index = _build_navigation_index(
        run_dir,
        shard_rows,
        max_file_bytes=max_file_bytes,
    )
    compact_summary = _build_compact_session_summary(
        bundle_manifest=bundle_manifest,
        shard_rows=shard_rows,
        run_summary=_find_run_summary(
            run_dir,
            critical_parts,
        ),
    )
    _write_json(
        run_dir / "overview" / "session-summary.json",
        compact_summary,
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
        "tradeTapeNormalization": {
            "rollingV1": "converted to delta_v1_exported_from_rolling",
            "nativeDeltaV1": "preserved",
        },
        "overview": {
            "sessionSummary": "overview/session-summary.json",
            "latencySummary": "overview/latency-summary.json",
            "sourceManifest": "overview/source-manifest.json",
            "sourceShardIndex": (
                "overview/source-shard-index.json"
            ),
            "criticalEventParts": critical_parts,
            "frameParts": overview_parts,
        },
        "index": navigation_index,
        "shards": shard_rows,
    }
    _write_json(run_dir / "manifest.json", manifest)

    (run_dir / "README.md").write_text(
        (
            "# Scalp bot session export\n\n"
            "This directory is an analysis-grade export. The lossless raw "
            "session remains local and is intentionally not committed.\n\n"
            "Start with manifest.json, overview/session-summary.json, "
            "and overview/latency-summary.json. Open only the referenced "
            "time shards when deeper tick/order-book inspection is needed.\n"
        ),
        encoding="utf-8",
    )

    return run_dir
