from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable

import msgspec

from .analysis_pack import (
    FOCUS_WINDOWS,
    FRAME_EVENTS,
    INTERACTION_FOCUS_STATES,
    _in_windows,
    _merge_windows,
    _row_ts,
    _trim_book,
)
from .long_run_pack import (
    CRITICAL_EVENTS,
    _compact_frame,
    _find_latency_trace,
    _prometheus_latency_summary,
)


_DECODER = msgspec.json.Decoder(type=dict)

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


def _iter_rows(path: Path) -> Iterable[dict]:
    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                value = _DECODER.decode(line)
            except Exception:
                continue
            if isinstance(value, dict):
                yield value


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


class _JsonlPartWriter:
    def __init__(
        self,
        *,
        output_dir: Path,
        root: Path,
        max_file_bytes: int,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
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
                "single JSONL row exceeds configured Git part size "
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


def _split_jsonl_stream(
    source: BinaryIO,
    *,
    output_dir: Path,
    root: Path,
    max_file_bytes: int,
) -> list[dict]:
    writer = _JsonlPartWriter(
        output_dir=output_dir,
        root=root,
        max_file_bytes=max_file_bytes,
    )
    for raw_line in source:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            writer.write(value)
    return writer.close()


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


def _activity_from_counts(counts: Counter[str]) -> dict:
    return {
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
        shard["activity"] = _activity_from_counts(counts)

    trade_parts = trade_writer.close()
    problem_parts = problem_writer.close()
    _write_json(
        index_dir / "shards.json",
        {
            "schemaVersion": 2,
            "shards": shard_rows,
        },
    )
    return {
        "shards": "index/shards.json",
        "tradeEventParts": trade_parts,
        "problemEventParts": problem_parts,
    }


def _scan_session(
    source: Path,
) -> tuple[
    Counter[str],
    set[str],
    dict[str, list[tuple[float, float]]],
    float,
    float,
    int,
    dict | None,
]:
    event_counts: Counter[str] = Counter()
    symbols: set[str] = set()
    focus: dict[str, list[tuple[float, float]]] = defaultdict(list)
    first_ts: float | None = None
    last_ts: float | None = None
    row_count = 0
    run_summary: dict | None = None

    for row in _iter_rows(source):
        row_count += 1
        ts = _row_ts(row)
        if ts > 0:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)

        event = str(row.get("event") or "")
        symbol = str(row.get("symbol") or "")
        event_counts[event] += 1
        if symbol:
            symbols.add(symbol)

        window = FOCUS_WINDOWS.get(event)
        if symbol and window:
            before, after = window
            focus[symbol].append(
                (ts - before, ts + after)
            )

        if event == "decision" and symbol:
            payload = row.get("payload") or {}
            strategy = str(payload.get("strategy") or "")
            details = payload.get("details") or {}
            trace = payload.get("trace") or {}
            state = str(
                details.get("state")
                or trace.get("state")
                or ""
            )
            if state in INTERACTION_FOCUS_STATES.get(
                strategy,
                set(),
            ):
                focus[symbol].append(
                    (ts - 3.0, ts + 12.0)
                )

        if (
            event == "run_summary"
            and isinstance(row.get("payload"), dict)
        ):
            run_summary = dict(row["payload"])

    if first_ts is None:
        first_ts = 0.0
    if last_ts is None:
        last_ts = first_ts

    return (
        event_counts,
        symbols,
        {
            symbol: _merge_windows(windows)
            for symbol, windows in focus.items()
        },
        first_ts,
        last_ts,
        row_count,
        run_summary,
    )


def _shard_id(index: int, shard_minutes: float) -> str:
    start_minute = int(round(index * shard_minutes))
    end_minute = int(round((index + 1) * shard_minutes))
    return f"{start_minute:04d}-{end_minute:04d}"


class _LatencyAccumulator:
    def __init__(self) -> None:
        self.trace_events = 0
        self._stages: dict[str, dict[str, float]] = {}

    def observe(self, trace: dict) -> None:
        durations = trace.get("durationsMs")
        if not isinstance(durations, dict):
            return
        self.trace_events += 1
        for stage, raw in durations.items():
            if not isinstance(raw, (int, float)) or raw < 0:
                continue
            value = float(raw)
            bucket = self._stages.setdefault(
                str(stage),
                {
                    "samples": 0.0,
                    "sumMs": 0.0,
                    "maxMs": 0.0,
                },
            )
            bucket["samples"] += 1
            bucket["sumMs"] += value
            bucket["maxMs"] = max(
                bucket["maxMs"],
                value,
            )

    def summary(
        self,
        *,
        prometheus_snapshot: dict | None,
    ) -> dict:
        stages = {}
        for stage, bucket in sorted(self._stages.items()):
            samples = int(bucket["samples"])
            stages[stage] = {
                "samples": samples,
                "meanMs": (
                    bucket["sumMs"] / samples
                    if samples
                    else None
                ),
                "maxMs": bucket["maxMs"],
            }
        return {
            "schemaVersion": 2,
            "traceEvents": self.trace_events,
            "stages": stages,
            "prometheusSummary": (
                _prometheus_latency_summary(
                    prometheus_snapshot
                )
            ),
            "detailPolicy": (
                "Individual latency traces are stored in bounded "
                "overview/latency-events JSONL parts."
            ),
        }


def _build_compact_session_summary(
    *,
    source_meta: dict,
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
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": source_meta,
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
            "Detailed events, trade tape, market frames and order books are "
            "stored in bounded indexes and time shards. No unbounded "
            "monolithic report is generated."
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
    """Stream a raw session into a bounded Git-friendly analysis tree.

    The exporter performs a lightweight first pass for focus-window discovery,
    then a streaming second pass that writes bounded files directly. It never
    builds the legacy monolithic session report or temporary ZIP bundles.
    """
    del horizon_seconds  # retained for CLI compatibility; no global hindsight report.

    source = Path(session_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if shard_minutes <= 0:
        raise ValueError("shard_minutes must be positive")
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")

    output_root = Path(output_root)
    run_dir = output_root / source.stem
    if run_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"Git export already exists: {run_dir}"
            )
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    (
        source_event_counts,
        source_symbols,
        focus,
        first_ts,
        last_ts,
        row_count,
        run_summary,
    ) = _scan_session(source)

    shard_seconds = shard_minutes * 60.0
    duration = max(0.0, last_ts - first_ts)
    shard_count = max(
        1,
        int(math.floor(duration / shard_seconds)) + 1,
    )
    source_meta = {
        "file": source.name,
        "sizeBytes": source.stat().st_size,
        "rows": row_count,
        "eventCounts": dict(source_event_counts),
        "symbols": sorted(source_symbols),
        "firstTs": first_ts,
        "lastTs": last_ts,
        "durationSeconds": duration,
    }

    overview_dir = run_dir / "overview"
    overview_writer = _JsonlPartWriter(
        output_dir=overview_dir / "frames",
        root=run_dir,
        max_file_bytes=max_file_bytes,
    )
    critical_writer = _JsonlPartWriter(
        output_dir=overview_dir / "critical-events",
        root=run_dir,
        max_file_bytes=max_file_bytes,
    )
    latency_writer = _JsonlPartWriter(
        output_dir=overview_dir / "latency-events",
        root=run_dir,
        max_file_bytes=max_file_bytes,
    )
    latency = _LatencyAccumulator()

    analysis_writers: list[_JsonlPartWriter] = []
    book_writers: list[_JsonlPartWriter] = []
    shard_dirs: list[Path] = []
    for index in range(shard_count):
        shard_dir = (
            run_dir
            / "shards"
            / _shard_id(index, shard_minutes)
        )
        shard_dirs.append(shard_dir)
        analysis_writers.append(
            _JsonlPartWriter(
                output_dir=shard_dir / "analysis",
                root=run_dir,
                max_file_bytes=max_file_bytes,
            )
        )
        book_writers.append(
            _JsonlPartWriter(
                output_dir=shard_dir / "orderbooks",
                root=run_dir,
                max_file_bytes=max_file_bytes,
            )
        )

    shard_rows_count = [0] * shard_count
    shard_frame_rows = [0] * shard_count
    shard_book_rows = [0] * shard_count
    shard_trade_gap_rows = [0] * shard_count
    last_overview_frame: dict[str, float] = {}
    last_shard_frame: dict[tuple[int, str], float] = {}
    last_book_sample: dict[tuple[int, str], float] = {}
    normalizer = _RollingTradeDeltaNormalizer()

    for raw_row in _iter_rows(source):
        event = str(raw_row.get("event") or "")
        symbol = str(raw_row.get("symbol") or "")
        ts = _row_ts(raw_row)
        payload = raw_row.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        shard_index = min(
            shard_count - 1,
            max(
                0,
                int((ts - first_ts) // shard_seconds)
                if ts >= first_ts
                else 0,
            ),
        )

        if event not in FRAME_EVENTS:
            overview_writer.write(raw_row)
            analysis_writers[shard_index].write(raw_row)
            shard_rows_count[shard_index] += 1

            if event in CRITICAL_EVENTS:
                critical_writer.write(raw_row)

            for trace in _find_latency_trace(payload):
                compact_trace = {
                    "ts": raw_row.get("ts"),
                    "iso": raw_row.get("iso"),
                    "symbol": raw_row.get("symbol"),
                    "eventId": trace.get("eventId"),
                    "traceId": trace.get("traceId"),
                    "topic": trace.get("topic"),
                    "exchangeTsMs": trace.get("exchangeTsMs"),
                    "durationsMs": trace.get("durationsMs"),
                }
                latency_writer.write(compact_trace)
                latency.observe(trace)
            continue

        if not symbol:
            continue

        normalized_row = normalizer.transform(raw_row)
        normalized_payload = normalized_row.get("payload")
        if not isinstance(normalized_payload, dict):
            normalized_payload = payload

        if bool(normalized_payload.get("tradeDeltaGap")):
            shard_trade_gap_rows[shard_index] += 1

        previous = last_overview_frame.get(symbol)
        if (
            previous is None
            or ts - previous >= overview_frame_seconds
        ):
            overview_writer.write(
                {
                    "ts": raw_row.get("ts"),
                    "iso": raw_row.get("iso"),
                    "event": "research_frame",
                    "symbol": symbol,
                    "payload": _compact_frame(
                        normalized_payload,
                        fast_depth=5,
                        deep_depth=5,
                        include_trade_delta=False,
                    ),
                }
            )
            last_overview_frame[symbol] = ts

        shard_key = (shard_index, symbol)
        previous = last_shard_frame.get(shard_key)
        if (
            previous is None
            or ts - previous >= shard_frame_seconds
        ):
            analysis_writers[shard_index].write(
                {
                    "ts": raw_row.get("ts"),
                    "iso": raw_row.get("iso"),
                    "event": "research_frame",
                    "symbol": symbol,
                    "payload": _compact_frame(
                        normalized_payload,
                        fast_depth=min(book_depth, 50),
                        deep_depth=book_depth,
                        include_trade_delta=True,
                    ),
                }
            )
            shard_rows_count[shard_index] += 1
            shard_frame_rows[shard_index] += 1
            last_shard_frame[shard_key] = ts

        is_focus = _in_windows(
            ts,
            focus.get(symbol, []),
        )
        previous_book = last_book_sample.get(shard_key)
        due_sample = (
            previous_book is None
            or ts - previous_book >= book_sample_seconds
        )
        if not is_focus and not due_sample:
            continue

        deep = (
            normalized_payload.get("deepOrderbook")
            or normalized_payload.get("orderbook")
        )
        if not isinstance(deep, dict):
            continue
        if not (deep.get("bids") or deep.get("asks")):
            continue

        depth = (
            focus_book_depth
            if is_focus
            else book_depth
        )
        book_writers[shard_index].write(
            {
                "ts": raw_row.get("ts"),
                "iso": raw_row.get("iso"),
                "event": "orderbook_sample",
                "symbol": symbol,
                "payload": {
                    "focus": is_focus,
                    "lastPrice": normalized_payload.get(
                        "lastPrice"
                    ),
                    "marketContext": normalized_payload.get(
                        "marketContext"
                    ),
                    "fastOrderbook": _trim_book(
                        normalized_payload.get(
                            "fastOrderbook"
                        )
                        or normalized_payload.get(
                            "orderbook"
                        ),
                        min(depth, 50),
                    ),
                    "deepOrderbook": _trim_book(
                        deep,
                        depth,
                    ),
                    "fastBookHealth": normalized_payload.get(
                        "fastBookHealth"
                    ),
                    "deepBookHealth": normalized_payload.get(
                        "deepBookHealth"
                    ),
                    "tradeFlow": normalized_payload.get(
                        "tradeFlow"
                    ),
                    "bookFlow": normalized_payload.get(
                        "bookFlow"
                    ),
                    "position": normalized_payload.get(
                        "position"
                    ),
                },
            }
        )
        shard_book_rows[shard_index] += 1
        last_book_sample[shard_key] = ts

    overview_parts = overview_writer.close()
    critical_parts = critical_writer.close()
    latency_parts = latency_writer.close()

    shard_rows: list[dict] = []
    for index, shard_dir in enumerate(shard_dirs):
        analysis_parts = analysis_writers[index].close()
        orderbook_parts = book_writers[index].close()
        core_start = first_ts + index * shard_seconds
        core_end = min(
            last_ts,
            core_start + shard_seconds,
        )
        shard_manifest = {
            "schemaVersion": 2,
            "source": source.name,
            "shard": index,
            "id": _shard_id(index, shard_minutes),
            "coreStartTs": core_start,
            "coreEndTs": core_end,
            "rows": shard_rows_count[index],
            "frameRows": shard_frame_rows[index],
            "orderbookRows": shard_book_rows[index],
            "tradeDeltaGapRows": shard_trade_gap_rows[index],
            "analysisParts": analysis_parts,
            "orderbookParts": orderbook_parts,
        }
        _write_json(
            shard_dir / "manifest.json",
            shard_manifest,
        )
        shard_rows.append(
            {
                **shard_manifest,
                "manifest": (
                    shard_dir / "manifest.json"
                ).relative_to(run_dir).as_posix(),
            }
        )

    navigation_index = _build_navigation_index(
        run_dir,
        shard_rows,
        max_file_bytes=max_file_bytes,
    )

    prometheus_snapshot = (
        run_summary.get("latencyMetrics")
        if isinstance(run_summary, dict)
        and isinstance(run_summary.get("latencyMetrics"), dict)
        else None
    )
    latency_summary = latency.summary(
        prometheus_snapshot=prometheus_snapshot,
    )
    _write_json(
        overview_dir / "latency-summary.json",
        latency_summary,
    )

    source_manifest = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": source_meta,
        "sharding": {
            "shardMinutes": shard_minutes,
            "shardCount": shard_count,
            "frameSeconds": shard_frame_seconds,
            "bookDepth": book_depth,
            "focusBookDepth": focus_book_depth,
            "orderbookSampleSeconds": book_sample_seconds,
        },
    }
    _write_json(
        overview_dir / "source-manifest.json",
        source_manifest,
    )
    _write_json(
        overview_dir / "source-shard-index.json",
        {
            "schemaVersion": 2,
            "shards": [
                {
                    "id": row["id"],
                    "coreStartTs": row["coreStartTs"],
                    "coreEndTs": row["coreEndTs"],
                    "rows": row["rows"],
                    "frameRows": row["frameRows"],
                    "orderbookRows": row["orderbookRows"],
                    "tradeDeltaGapRows": row[
                        "tradeDeltaGapRows"
                    ],
                }
                for row in shard_rows
            ],
        },
    )

    compact_summary = _build_compact_session_summary(
        source_meta=source_meta,
        shard_rows=shard_rows,
        run_summary=run_summary,
    )
    _write_json(
        overview_dir / "session-summary.json",
        compact_summary,
    )

    manifest = {
        "schemaVersion": 2,
        "format": "scalp-bot-git-session-export",
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": source_meta,
        "exportMode": "two-pass-streaming",
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
            "latencyEventParts": latency_parts,
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
            "This directory is a bounded analysis-grade export produced "
            "directly from the raw session without temporary ZIP bundles or "
            "a monolithic session report.\n\n"
            "Start with manifest.json, overview/session-summary.json, "
            "overview/latency-summary.json and index/shards.json. Open only "
            "the referenced time shards when deeper tick/order-book "
            "inspection is needed.\n"
        ),
        encoding="utf-8",
    )

    return run_dir
