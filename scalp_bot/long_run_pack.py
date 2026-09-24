from __future__ import annotations

import json
import math
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

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
from .recorder import SessionRecorder


_DECODER = msgspec.json.Decoder(type=dict)

CRITICAL_EVENTS = {
    "bot_started",
    "run_summary",
    "scanner_error",
    "context_error",
    "fast_path_error",
    "strategy_error",
    "entry_pending",
    "entry_add_pending",
    "entry_cancelled",
    "trade_opened",
    "position_added",
    "partial_take",
    "trade_closed",
    "risk_reject",
    "setup_blocked",
    "arbiter_blocked",
    "economic_shadow",
    "research_policy_shadow",
    "research_policy_blocked",
    "setup_consumed",
    "setup_rearmed",
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


def _json_line(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def _compact_frame(
    payload: dict,
    *,
    fast_depth: int,
    deep_depth: int,
    include_trade_delta: bool,
) -> dict:
    fast = (
        payload.get("fastOrderbook")
        or payload.get("orderbook")
        or {}
    )
    deep = (
        payload.get("deepOrderbook")
        or payload.get("orderbook")
        or {}
    )
    result = {
        "lastPrice": payload.get("lastPrice"),
        "trend": payload.get("trend"),
        "marketContext": payload.get("marketContext"),
        "analysisRuntime": payload.get("analysisRuntime"),
        "candle": payload.get("candle"),
        "orderbook": _trim_book(fast, fast_depth),
        "fastOrderbook": _trim_book(fast, fast_depth),
        "deepOrderbook": _trim_book(deep, deep_depth),
        "bookHealth": payload.get("bookHealth"),
        "fastBookHealth": payload.get("fastBookHealth"),
        "deepBookHealth": payload.get("deepBookHealth"),
        "candleHealth": payload.get("candleHealth"),
        "tradeFlow": payload.get("tradeFlow"),
        "bookFlow": payload.get("bookFlow"),
        "position": payload.get("position"),
        "structure": payload.get("structure"),
        "tradeEncoding": payload.get("tradeEncoding"),
        "tradeCursor": payload.get("tradeCursor"),
        "tradeDeltaFromSequence": payload.get(
            "tradeDeltaFromSequence"
        ),
        "tradeDeltaGap": payload.get("tradeDeltaGap"),
    }
    if include_trade_delta:
        result["recentTrades"] = list(
            payload.get("recentTrades") or []
        )
    return result


def _find_latency_trace(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        durations = value.get("durationsMs")
        if (
            value.get("eventId")
            and isinstance(durations, dict)
        ):
            yield value
        for child in value.values():
            yield from _find_latency_trace(child)
    elif isinstance(value, list):
        for child in value:
            yield from _find_latency_trace(child)


def _trace_quality(trace: dict) -> tuple[int, int]:
    durations = trace.get("durationsMs") or {}
    filled = sum(
        value is not None
        for value in durations.values()
    )
    timestamps = sum(
        trace.get(key) is not None
        for key in (
            "receiptTsNs",
            "parsedTsNs",
            "processorTsNs",
            "bookUpdatedTsNs",
            "featuresReadyTsNs",
            "strategyEvalTsNs",
            "fireTsNs",
            "orderSentTsNs",
            "orderAckTsNs",
            "fillTsNs",
        )
    )
    return filled, timestamps


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    rows = sorted(values)
    if len(rows) == 1:
        return rows[0]
    pos = (len(rows) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return rows[lo]
    weight = pos - lo
    return rows[lo] * (1 - weight) + rows[hi] * weight


def _histogram_percentile_ms(
    row: dict,
    q: float,
) -> float | None:
    count = row.get("count")
    buckets = row.get("buckets")
    if (
        not isinstance(count, (int, float))
        or count <= 0
        or not isinstance(buckets, dict)
    ):
        return None
    target = float(count) * q
    finite = []
    for raw_le, raw_count in buckets.items():
        if raw_le == "+Inf":
            continue
        try:
            upper = float(raw_le)
            cumulative = float(raw_count)
        except (TypeError, ValueError):
            continue
        finite.append((upper, cumulative))
    for upper, cumulative in sorted(finite):
        if cumulative >= target:
            return upper * 1000
    return None


def _prometheus_latency_summary(
    snapshot: dict | None,
) -> list[dict]:
    if not isinstance(snapshot, dict):
        return []
    rows = snapshot.get("latencyHistogram")
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        result.append({
            "labels": labels,
            "samples": row.get("count"),
            "meanMs": (
                float(row.get("sum")) * 1000
                / float(row.get("count"))
                if (
                    isinstance(row.get("sum"), (int, float))
                    and isinstance(row.get("count"), (int, float))
                    and float(row.get("count")) > 0
                )
                else None
            ),
            "p50Ms": _histogram_percentile_ms(
                row,
                0.50,
            ),
            "p95Ms": _histogram_percentile_ms(
                row,
                0.95,
            ),
            "p99Ms": _histogram_percentile_ms(
                row,
                0.99,
            ),
        })
    return result


def _latency_summary(
    traces: dict[str, dict],
    *,
    histogram_snapshot: dict | None = None,
) -> dict:
    by_stage: dict[str, list[float]] = defaultdict(list)
    rows: list[dict] = []
    for event_id, trace in sorted(traces.items()):
        durations = trace.get("durationsMs") or {}
        compact = {
            "eventId": event_id,
            "traceId": trace.get("traceId"),
            "topic": trace.get("topic"),
            "exchangeTsMs": trace.get("exchangeTsMs"),
            "durationsMs": durations,
        }
        rows.append(compact)
        for stage, value in durations.items():
            if isinstance(value, (int, float)) and value >= 0:
                by_stage[str(stage)].append(float(value))

    stages = {}
    for stage, values in sorted(by_stage.items()):
        stages[stage] = {
            "samples": len(values),
            "p50Ms": _percentile(values, 0.50),
            "p95Ms": _percentile(values, 0.95),
            "p99Ms": _percentile(values, 0.99),
            "maxMs": max(values) if values else None,
            "meanMs": (
                sum(values) / len(values)
                if values
                else None
            ),
        }
    return {
        "traceEvents": len(rows),
        "stages": stages,
        "events": rows,
        "prometheusSummary": (
            _prometheus_latency_summary(
                histogram_snapshot
            )
        ),
        "prometheusSnapshot": (
            histogram_snapshot
            if isinstance(histogram_snapshot, dict)
            else None
        ),
    }


def _focus_windows(
    source: Path,
) -> tuple[
    Counter[str],
    set[str],
    dict[str, list[tuple[float, float]]],
    float,
    float,
    int,
    dict[str, dict],
    dict | None,
]:
    event_counts: Counter[str] = Counter()
    symbols: set[str] = set()
    focus: dict[str, list[tuple[float, float]]] = (
        defaultdict(list)
    )
    first_ts: float | None = None
    last_ts: float | None = None
    row_count = 0
    latency_traces: dict[str, dict] = {}
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

        if event == "run_summary":
            payload = row.get("payload") or {}
            if isinstance(payload, dict):
                run_summary = dict(payload)

        if event not in FRAME_EVENTS:
            payload = row.get("payload") or {}
            for trace in _find_latency_trace(payload):
                event_id = str(trace.get("eventId") or "")
                if not event_id:
                    continue
                previous = latency_traces.get(event_id)
                if (
                    previous is None
                    or _trace_quality(trace)
                    > _trace_quality(previous)
                ):
                    latency_traces[event_id] = trace

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
        latency_traces,
        run_summary,
    )


def build_long_run_analysis_bundle(
    session_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    shard_seconds: float = 3600.0,
    overview_frame_seconds: float = 10.0,
    shard_frame_seconds: float = 3.0,
    overview_book_depth: int = 5,
    shard_book_depth: int = 16,
    focus_book_depth: int = 50,
    orderbook_sample_seconds: float = 15.0,
    opportunity_horizon_seconds: float = 120.0,
) -> Path:
    source = Path(session_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if shard_seconds <= 0:
        raise ValueError("shard_seconds must be positive")

    (
        event_counts,
        symbols,
        focus,
        first_ts,
        last_ts,
        row_count,
        latency_traces,
        run_summary,
    ) = _focus_windows(source)

    duration = max(0.0, last_ts - first_ts)
    shard_count = max(
        1,
        int(math.floor(duration / shard_seconds)) + 1,
    )

    bundle_dir = (
        Path(output_dir)
        if output_dir is not None
        else source.with_name(
            f"{source.stem}-analysis-bundle"
        )
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="scalp-long-run-",
    ) as tmp_name:
        tmp = Path(tmp_name)
        overview_path = tmp / "overview-analysis.jsonl"
        critical_path = tmp / "critical-events.jsonl"

        shard_dirs = []
        shard_analysis = []
        shard_books = []
        for index in range(shard_count):
            path = tmp / f"shard-{index:02d}"
            path.mkdir(parents=True, exist_ok=True)
            shard_dirs.append(path)
            shard_analysis.append(
                (path / "session-analysis.jsonl").open(
                    "w",
                    encoding="utf-8",
                )
            )
            shard_books.append(
                (path / "focus-orderbooks.jsonl").open(
                    "w",
                    encoding="utf-8",
                )
            )

        overview_rows = 0
        critical_rows = 0
        shard_rows = [0] * shard_count
        shard_frame_rows = [0] * shard_count
        shard_book_rows = [0] * shard_count
        last_overview_frame: dict[str, float] = {}
        last_shard_frame: dict[tuple[int, str], float] = {}
        last_book_sample: dict[tuple[int, str], float] = {}

        try:
            with overview_path.open(
                "w",
                encoding="utf-8",
            ) as overview_fh, critical_path.open(
                "w",
                encoding="utf-8",
            ) as critical_fh:
                for row in _iter_rows(source):
                    event = str(row.get("event") or "")
                    symbol = str(row.get("symbol") or "")
                    ts = _row_ts(row)
                    payload = row.get("payload") or {}
                    shard_index = min(
                        shard_count - 1,
                        max(
                            0,
                            int(
                                (ts - first_ts)
                                // shard_seconds
                            )
                            if ts >= first_ts
                            else 0,
                        ),
                    )

                    if event not in FRAME_EVENTS:
                        line = _json_line(row)
                        overview_fh.write(line)
                        overview_rows += 1
                        shard_analysis[
                            shard_index
                        ].write(line)
                        shard_rows[shard_index] += 1
                        if event in CRITICAL_EVENTS:
                            critical_fh.write(line)
                            critical_rows += 1
                        continue

                    if not symbol:
                        continue

                    previous = last_overview_frame.get(
                        symbol
                    )
                    if (
                        previous is None
                        or ts - previous
                        >= overview_frame_seconds
                    ):
                        compact = {
                            "ts": row.get("ts"),
                            "iso": row.get("iso"),
                            "event": "research_frame",
                            "symbol": symbol,
                            "payload": _compact_frame(
                                payload,
                                fast_depth=overview_book_depth,
                                deep_depth=overview_book_depth,
                                include_trade_delta=False,
                            ),
                        }
                        overview_fh.write(
                            _json_line(compact)
                        )
                        overview_rows += 1
                        last_overview_frame[symbol] = ts

                    shard_key = (
                        shard_index,
                        symbol,
                    )
                    previous = last_shard_frame.get(
                        shard_key
                    )
                    if (
                        previous is None
                        or ts - previous
                        >= shard_frame_seconds
                    ):
                        compact = {
                            "ts": row.get("ts"),
                            "iso": row.get("iso"),
                            "event": "research_frame",
                            "symbol": symbol,
                            "payload": _compact_frame(
                                payload,
                                fast_depth=min(
                                    shard_book_depth,
                                    50,
                                ),
                                deep_depth=shard_book_depth,
                                include_trade_delta=True,
                            ),
                        }
                        shard_analysis[
                            shard_index
                        ].write(
                            _json_line(compact)
                        )
                        shard_rows[shard_index] += 1
                        shard_frame_rows[
                            shard_index
                        ] += 1
                        last_shard_frame[
                            shard_key
                        ] = ts

                    is_focus = _in_windows(
                        ts,
                        focus.get(symbol, []),
                    )
                    previous_book = last_book_sample.get(
                        shard_key
                    )
                    due_sample = (
                        previous_book is None
                        or ts - previous_book
                        >= orderbook_sample_seconds
                    )
                    if not is_focus and not due_sample:
                        continue

                    deep = (
                        payload.get("deepOrderbook")
                        or payload.get("orderbook")
                    )
                    if not isinstance(deep, dict):
                        continue
                    if not (
                        deep.get("bids")
                        or deep.get("asks")
                    ):
                        continue

                    depth = (
                        focus_book_depth
                        if is_focus
                        else shard_book_depth
                    )
                    sample = {
                        "ts": row.get("ts"),
                        "iso": row.get("iso"),
                        "event": "orderbook_sample",
                        "symbol": symbol,
                        "payload": {
                            "focus": is_focus,
                            "lastPrice": payload.get(
                                "lastPrice"
                            ),
                            "marketContext": payload.get(
                                "marketContext"
                            ),
                            "fastOrderbook": _trim_book(
                                payload.get(
                                    "fastOrderbook"
                                )
                                or payload.get(
                                    "orderbook"
                                ),
                                min(depth, 50),
                            ),
                            "deepOrderbook": _trim_book(
                                deep,
                                depth,
                            ),
                            "fastBookHealth": payload.get(
                                "fastBookHealth"
                            ),
                            "deepBookHealth": payload.get(
                                "deepBookHealth"
                            ),
                            "tradeFlow": payload.get(
                                "tradeFlow"
                            ),
                            "bookFlow": payload.get(
                                "bookFlow"
                            ),
                            "position": payload.get(
                                "position"
                            ),
                        },
                    }
                    shard_books[
                        shard_index
                    ].write(_json_line(sample))
                    shard_book_rows[
                        shard_index
                    ] += 1
                    last_book_sample[
                        shard_key
                    ] = ts
        finally:
            for fh in [
                *shard_analysis,
                *shard_books,
            ]:
                fh.close()

        report_path = (
            SessionRecorder.write_report_for_file(
                overview_path,
                horizon_seconds=(
                    opportunity_horizon_seconds
                ),
            )
        )
        final_report = tmp / "session-report.json"
        report_path.replace(final_report)

        latency_summary = _latency_summary(
            latency_traces,
            histogram_snapshot=(
                run_summary.get("latencyMetrics")
                if isinstance(run_summary, dict)
                else None
            ),
        )
        latency_path = tmp / "latency-summary.json"
        latency_path.write_text(
            json.dumps(
                latency_summary,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        shard_index_rows = []
        for index, shard_dir in enumerate(shard_dirs):
            core_start = first_ts + index * shard_seconds
            core_end = min(
                last_ts,
                core_start + shard_seconds,
            )
            shard_manifest = {
                "schemaVersion": 1,
                "source": source.name,
                "shard": index,
                "coreStartTs": core_start,
                "coreEndTs": core_end,
                "rows": shard_rows[index],
                "frameRows": shard_frame_rows[index],
                "orderbookRows": shard_book_rows[index],
                "symbols": sorted(symbols),
                "tradeTape": (
                    "delta_v1 preserved in "
                    "session-analysis.jsonl"
                ),
            }
            manifest_path = (
                shard_dir / "manifest.json"
            )
            manifest_path.write_text(
                json.dumps(
                    shard_manifest,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            zip_name = (
                f"{source.stem}-hour-{index:02d}.zip"
            )
            zip_path = bundle_dir / zip_name
            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for name in (
                    "manifest.json",
                    "session-analysis.jsonl",
                    "focus-orderbooks.jsonl",
                ):
                    archive.write(
                        shard_dir / name,
                        arcname=name,
                    )

            shard_index_rows.append({
                **shard_manifest,
                "archive": zip_name,
                "archiveSizeBytes": (
                    zip_path.stat().st_size
                ),
            })

        shard_index_path = tmp / "shard-index.json"
        shard_index_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "shards": shard_index_rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        manifest = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(
                UTC
            ).isoformat(),
            "source": {
                "file": source.name,
                "sizeBytes": source.stat().st_size,
                "rows": row_count,
                "eventCounts": dict(event_counts),
                "symbols": sorted(symbols),
                "firstTs": first_ts,
                "lastTs": last_ts,
                "durationSeconds": duration,
            },
            "overview": {
                "frameSeconds": overview_frame_seconds,
                "bookDepth": overview_book_depth,
                "rows": overview_rows,
                "criticalRows": critical_rows,
                "report": "session-report.json",
                "latencySummary": "latency-summary.json",
            },
            "sharding": {
                "shardSeconds": shard_seconds,
                "shardCount": shard_count,
                "frameSeconds": shard_frame_seconds,
                "bookDepth": shard_book_depth,
                "focusBookDepth": focus_book_depth,
                "orderbookSampleSeconds": (
                    orderbook_sample_seconds
                ),
                "index": "shard-index.json",
            },
            "analysisWorkflow": [
                "Upload the overview ZIP first.",
                (
                    "Use session-report.json and "
                    "latency-summary.json to identify "
                    "strategies, trades, errors and hours "
                    "that need deeper inspection."
                ),
                (
                    "Upload only the referenced hourly "
                    "shard ZIPs for tick/DOM reconstruction."
                ),
                (
                    "Keep the original raw JSONL locally "
                    "as the lossless source of truth."
                ),
            ],
        }
        manifest_path = tmp / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        readme = tmp / "README.txt"
        readme.write_text(
            (
                "LONG-RUN ANALYSIS BUNDLE\n\n"
                "Start with this overview ZIP. It contains "
                "the global report, critical events, latency "
                "summary, a sampled overview stream, and an "
                "index of hourly shard archives.\n\n"
                "Do not upload the raw session unless a "
                "specific datum is missing. After overview "
                "analysis, upload only the hour-XX shard(s) "
                "identified in shard-index.json. Each shard "
                "preserves delta trade tape and deeper DOM "
                "samples for that hour.\n"
            ),
            encoding="utf-8",
        )

        overview_zip = bundle_dir / (
            f"{source.stem}-overview.zip"
        )
        with zipfile.ZipFile(
            overview_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path, name in (
                (manifest_path, "manifest.json"),
                (readme, "README.txt"),
                (final_report, "session-report.json"),
                (
                    critical_path,
                    "critical-events.jsonl",
                ),
                (
                    latency_path,
                    "latency-summary.json",
                ),
                (
                    shard_index_path,
                    "shard-index.json",
                ),
                (
                    overview_path,
                    "overview-analysis.jsonl",
                ),
            ):
                archive.write(path, arcname=name)

        (bundle_dir / "bundle-manifest.json").write_text(
            json.dumps(
                {
                    **manifest,
                    "overviewArchive": (
                        overview_zip.name
                    ),
                    "overviewArchiveSizeBytes": (
                        overview_zip.stat().st_size
                    ),
                    "shards": shard_index_rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return bundle_dir
