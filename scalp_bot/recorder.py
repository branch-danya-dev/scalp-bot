from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock


class SessionRecorder:
    def __init__(self, directory: str) -> None:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = root / f"session-{stamp}.jsonl"
        self._lock = Lock()

    def record(self, event: str, symbol: str | None, payload: dict) -> None:
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "symbol": symbol,
            "payload": payload,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
