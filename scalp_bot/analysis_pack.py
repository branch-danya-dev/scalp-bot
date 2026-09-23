from __future__ import annotations

import json
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .recorder import SessionRecorder


FRAME_EVENTS = {"research_frame", "market_frame"}
FOCUS_WINDOWS = {
    "entry_pending": (10.0, 30.0),
    "entry_cancelled": (15.0, 15.0),
    "trade_opened": (30.0, 60.0),
    "partial_take": (20.0, 40.0),
    "trade_closed": (30.0, 60.0),
    "risk_reject": (5.0, 15.0),
    "setup_blocked": (5.0, 15.0),
    "arbiter_blocked": (5.0, 15.0),
    "economic_shadow": (5.0, 10.0),
    "entry_freshness_changed": (5.0, 10.0),
    "strategy_error": (5.0, 10.0),
}

INTERACTION_FOCUS_STATES = {
    "trend_structure": {"test", "reclaim", "continuation"},
    "weak_level_rejection": {"test", "reject", "reaction"},
    "orderbook_density": {"test", "defended", "reaction", "exhausted"},
    "level_breakout": {"break", "impulse"},
}


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _iter_rows(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _merge_windows(rows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not rows:
        return []
    result: list[tuple[float, float]] = []
    for start, end in sorted(rows):
        if not result or start > result[-1][1]:
            result.append((start, end))
            continue
        previous_start, previous_end = result[-1]
        result[-1] = (previous_start, max(previous_end, end))
    return result


def _in_windows(ts: float, windows: list[tuple[float, float]]) -> bool:
    for start, end in windows:
        if ts < start:
            return False
        if start <= ts <= end:
            return True
    return False


def _trim_book(book: dict | None, depth: int) -> dict:
    if not isinstance(book, dict):
        return {}
    bids = list(book.get("bids") or [])[:depth]
    asks = list(book.get("asks") or [])[:depth]
    return {
        "bids": bids,
        "asks": asks,
        "bestBid": book.get("bestBid"),
        "bestAsk": book.get("bestAsk"),
        "spreadPct": book.get("spreadPct"),
    }


def _compact_frame_payload(payload: dict, book_depth: int) -> dict:
    return {
        "lastPrice": payload.get("lastPrice"),
        "trend": payload.get("trend"),
        "marketContext": payload.get("marketContext"),
        "candle": payload.get("candle"),
        "orderbook": _trim_book(payload.get("orderbook"), book_depth),
        "bookHealth": payload.get("bookHealth"),
        "tradeFlow": payload.get("tradeFlow"),
        "bookFlow": payload.get("bookFlow"),
        "position": payload.get("position"),
    }


def build_analysis_pack(
    session_path: str | Path,
    *,
    output_path: str | Path | None = None,
    opportunity_horizon_seconds: float = 120.0,
    compact_frame_seconds: float = 1.0,
    compact_book_depth: int = 5,
    orderbook_sample_seconds: float = 5.0,
    orderbook_sample_depth: int = 16,
    focus_orderbook_depth: int = 50,
) -> Path:
    source = Path(session_path)
    if not source.is_file():
        raise FileNotFoundError(source)

    event_counts: Counter[str] = Counter()
    symbols: set[str] = set()
    focus: dict[str, list[tuple[float, float]]] = defaultdict(list)
    row_count = 0

    # Pass 1 only identifies event windows; no large market payload is retained.
    for row in _iter_rows(source):
        row_count += 1
        event = str(row.get("event") or "")
        symbol = str(row.get("symbol") or "")
        event_counts[event] += 1
        if symbol:
            symbols.add(symbol)
        window = FOCUS_WINDOWS.get(event)
        if symbol and window:
            ts = _row_ts(row)
            before, after = window
            focus[symbol].append((ts - before, ts + after))

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
            if state in INTERACTION_FOCUS_STATES.get(strategy, set()):
                ts = _row_ts(row)
                focus[symbol].append((ts - 3.0, ts + 12.0))

    focus = {
        symbol: _merge_windows(windows)
        for symbol, windows in focus.items()
    }

    if output_path is None:
        output = source.with_name(f"{source.stem}-analysis-pack.zip")
    else:
        output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    compact_rows = 0
    orderbook_rows = 0
    full_focus_orderbook_rows = 0
    last_compact_ts: dict[str, float] = {}
    last_book_ts: dict[str, float] = {}

    with tempfile.TemporaryDirectory(prefix="scalp-analysis-") as tmp_name:
        tmp = Path(tmp_name)
        compact_path = tmp / "session-analysis.jsonl"
        orderbook_path = tmp / "orderbook-samples.jsonl"

        with compact_path.open("w", encoding="utf-8") as compact_fh, orderbook_path.open("w", encoding="utf-8") as book_fh:
            for row in _iter_rows(source):
                event = str(row.get("event") or "")
                symbol = str(row.get("symbol") or "")
                ts = _row_ts(row)
                payload = row.get("payload") or {}

                if event not in FRAME_EVENTS:
                    compact_fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    compact_rows += 1
                    continue

                if not symbol:
                    continue

                previous = last_compact_ts.get(symbol)
                if previous is None or ts - previous >= compact_frame_seconds:
                    compact_row = {
                        "ts": row.get("ts"),
                        "iso": row.get("iso"),
                        "event": "research_frame",
                        "symbol": symbol,
                        "payload": _compact_frame_payload(
                            payload,
                            compact_book_depth,
                        ),
                    }
                    compact_fh.write(json.dumps(compact_row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    compact_rows += 1
                    last_compact_ts[symbol] = ts

                is_focus = _in_windows(ts, focus.get(symbol, []))
                previous_book = last_book_ts.get(symbol)
                due_sample = (
                    previous_book is None
                    or ts - previous_book >= orderbook_sample_seconds
                )
                if not is_focus and not due_sample:
                    continue
                book = payload.get("orderbook")
                if not isinstance(book, dict) or not (book.get("bids") or book.get("asks")):
                    continue

                depth = focus_orderbook_depth if is_focus else orderbook_sample_depth
                sample = {
                    "ts": row.get("ts"),
                    "iso": row.get("iso"),
                    "event": "orderbook_sample",
                    "symbol": symbol,
                    "payload": {
                        "focus": is_focus,
                        "lastPrice": payload.get("lastPrice"),
                        "trend": payload.get("trend"),
                        "marketContext": payload.get("marketContext"),
                        "orderbook": _trim_book(book, depth),
                        "bookHealth": payload.get("bookHealth"),
                        "tradeFlow": payload.get("tradeFlow"),
                        "bookFlow": payload.get("bookFlow"),
                        "position": payload.get("position"),
                    },
                }
                book_fh.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
                orderbook_rows += 1
                if is_focus:
                    full_focus_orderbook_rows += 1
                last_book_ts[symbol] = ts

        report_path = SessionRecorder.write_report_for_file(
            compact_path,
            horizon_seconds=opportunity_horizon_seconds,
        )
        final_report_path = tmp / "session-report.json"
        report_path.replace(final_report_path)

        manifest = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat(),
            "source": {
                "file": source.name,
                "sizeBytes": source.stat().st_size,
                "rows": row_count,
                "eventCounts": dict(event_counts),
                "symbols": sorted(symbols),
            },
            "compaction": {
                "compactFrameSeconds": compact_frame_seconds,
                "compactBookDepth": compact_book_depth,
                "orderbookSampleSeconds": orderbook_sample_seconds,
                "orderbookSampleDepth": orderbook_sample_depth,
                "focusOrderbookDepth": focus_orderbook_depth,
                "opportunityHorizonSeconds": opportunity_horizon_seconds,
                "compactRows": compact_rows,
                "orderbookRows": orderbook_rows,
                "focusOrderbookRows": full_focus_orderbook_rows,
                "focusEvents": sorted(FOCUS_WINDOWS),
                "interactionDecisionFocusStates": {
                    key: sorted(values)
                    for key, values in INTERACTION_FOCUS_STATES.items()
                },
            },
            "contents": {
                "session-analysis.jsonl": "All non-frame events plus 1s compact market frames; sufficient for Opportunity Review, Market Interaction Research and trade reconstruction.",
                "session-report.json": "Derived post-run report: opportunity review, market interaction research, closed trades, trade reviews, coins and chart coverage.",
                "orderbook-samples.jsonl": "Sampled order books globally, with denser/deeper snapshots around trades and advanced interaction-state transitions.",
            },
            "rawSourceRequiredFor": [
                "tick-perfect reconstruction outside sampled windows",
                "full 1000-level order-book history outside focus windows",
                "auditing any datum intentionally removed by compaction",
            ],
        }
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (tmp / "README.txt").write_text(
            "This is a compact analysis pack. Keep the original session JSONL locally as the lossless source of truth.\n"
            "Use session-report.json for the derived summary and market-interaction research, session-analysis.jsonl for decisions/candles/reconstruction,\n"
            "and orderbook-samples.jsonl for DOM analysis around important events, advanced interaction states and periodic market samples.\n",
            encoding="utf-8",
        )

        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name in (
                "manifest.json",
                "README.txt",
                "session-report.json",
                "session-analysis.jsonl",
                "orderbook-samples.jsonl",
            ):
                archive.write(tmp / name, arcname=name)

    return output
