from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import time


class SessionRecorder:
    def __init__(self, directory: str) -> None:
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.root / f"session-{stamp}.jsonl"
        self._lock = Lock()

    def record(self, event: str, symbol: str | None, payload: dict) -> None:
        row = {
            "ts": time(),
            "iso": datetime.now(UTC).isoformat(),
            "event": event,
            "symbol": symbol,
            "payload": payload,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def list_sessions(self) -> list[dict]:
        sessions: list[dict] = []
        for path in sorted(self.root.glob("session-*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = path.stat()
            sessions.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        return sessions

    def replay_bundle(self, name: str, symbol: str | None = None) -> dict:
        path = self._safe_path(name)
        rows = self._read_rows(path)
        symbols = sorted({row.get("symbol") for row in rows if row.get("symbol")})
        if symbol not in symbols:
            symbol = symbols[0] if symbols else None
        if symbol is None:
            return {"name": name, "symbols": [], "symbol": None, "bootstrapCandles": [], "frames": [], "events": []}

        symbol_rows = [row for row in rows if row.get("symbol") == symbol]
        bootstrap: list[dict] = []
        frames: list[dict] = []
        events: list[dict] = []
        for row in symbol_rows:
            event = row.get("event")
            payload = row.get("payload") or {}
            if event == "symbol_activated" and not bootstrap:
                market = payload.get("market") or {}
                bootstrap = market.get("candles") or []
            if event == "market_frame":
                frames.append({"ts": self._row_ts(row), **payload})
            else:
                events.append({"ts": self._row_ts(row), "event": event, "symbol": symbol, "payload": payload})

        return {
            "name": name,
            "symbols": symbols,
            "symbol": symbol,
            "bootstrapCandles": bootstrap,
            "frames": frames,
            "events": events,
        }

    def _safe_path(self, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("invalid session name")
        path = self.root / name
        if not path.is_file() or not name.startswith("session-") or not name.endswith(".jsonl"):
            raise FileNotFoundError(name)
        return path

    @staticmethod
    def _row_ts(row: dict) -> float:
        raw = row.get("ts")
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    @staticmethod
    def _read_rows(path: Path) -> list[dict]:
        rows: list[dict] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows
