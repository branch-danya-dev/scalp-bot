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


    def _session_path(
        self,
        name: str | None = None,
    ) -> Path:
        if name in {None, "", "current"}:
            return self.path
        return self._safe_path(name)

    @staticmethod
    def _setup_id_from_open(payload: dict) -> str | None:
        plan = payload.get("plan") or {}
        return (
            plan.get("setup_id")
            or plan.get("setupId")
            or payload.get("setupId")
        )

    @staticmethod
    def _setup_id_from_close(payload: dict) -> str | None:
        return (
            payload.get("setupId")
            or payload.get("setup_id")
        )

    @staticmethod
    def _merge_candles(*snapshots: dict | None) -> list[dict]:
        by_time: dict[int, dict] = {}
        for snapshot in snapshots:
            market = (
                snapshot.get("market")
                if isinstance(snapshot, dict)
                else None
            )
            if not isinstance(market, dict):
                continue
            for candle in market.get("candles") or []:
                if not isinstance(candle, dict):
                    continue
                raw_time = candle.get("time")
                if raw_time is None:
                    continue
                by_time[int(raw_time)] = candle
        return [by_time[key] for key in sorted(by_time)]

    @classmethod
    def _build_trade_reviews(
        cls,
        rows: list[dict],
        *,
        pre_roll_seconds: float = 120.0,
        post_roll_seconds: float = 30.0,
    ) -> list[dict]:
        pending: dict[tuple[str, str | None], list[dict]] = {}
        pairs: list[tuple[dict, dict]] = []

        for row in rows:
            symbol = str(row.get("symbol") or "")
            payload = row.get("payload") or {}
            event = row.get("event")
            if event == "trade_opened":
                setup_id = cls._setup_id_from_open(payload)
                pending.setdefault((symbol, setup_id), []).append(row)
                continue
            if event != "trade_closed":
                continue
            setup_id = cls._setup_id_from_close(payload)
            queue = pending.get((symbol, setup_id))
            if not queue and setup_id is not None:
                queue = next(
                    (
                        rows_for_key
                        for (key_symbol, _key_setup), rows_for_key
                        in pending.items()
                        if key_symbol == symbol and rows_for_key
                    ),
                    None,
                )
            if queue:
                open_row = queue.pop(0)
                pairs.append((open_row, row))

        reviews: list[dict] = []
        timeline_events = {
            "decision",
            "risk_reject",
            "setup_blocked",
            "trade_opened",
            "partial_take",
            "trade_closed",
            "setup_consumed",
            "setup_rearmed",
            "strategy_error",
        }
        for index, (open_row, close_row) in enumerate(pairs, start=1):
            symbol = str(open_row.get("symbol") or close_row.get("symbol") or "")
            open_payload = open_row.get("payload") or {}
            close_payload = close_row.get("payload") or {}
            plan = open_payload.get("plan") or {}
            open_ts = cls._row_ts(open_row)
            close_ts = cls._row_ts(close_row)
            setup_id = (
                cls._setup_id_from_close(close_payload)
                or cls._setup_id_from_open(open_payload)
            )
            review_id = f"{int(open_ts * 1000)}-{symbol}-{index}"
            window_start = open_ts - pre_roll_seconds
            window_end = close_ts + post_roll_seconds

            timeline: list[dict] = []
            research_frames: list[dict] = []
            market_frames: list[dict] = []
            for row in rows:
                if row.get("symbol") != symbol:
                    continue
                ts = cls._row_ts(row)
                if ts < window_start or ts > window_end:
                    continue
                event = row.get("event")
                payload = row.get("payload") or {}
                if event in timeline_events:
                    timeline.append({
                        "ts": ts,
                        "event": event,
                        "payload": payload,
                    })
                elif event == "research_frame":
                    research_frames.append({"ts": ts, **payload})
                elif event == "market_frame":
                    market_frames.append({"ts": ts, **payload})

            frames = research_frames or market_frames
            candles = cls._merge_candles(
                open_payload,
                close_payload,
            )
            summary = {
                "reviewId": review_id,
                "symbol": symbol,
                "strategy": (
                    close_payload.get("strategy")
                    or plan.get("strategy")
                ),
                "side": (
                    close_payload.get("side")
                    or plan.get("side")
                ),
                "setupId": setup_id,
                "openedAt": (
                    close_payload.get("openedAt")
                    or open_ts
                ),
                "closedAt": (
                    close_payload.get("closedAt")
                    or close_ts
                ),
                "durationSeconds": max(0.0, close_ts - open_ts),
                "entry": close_payload.get("entry"),
                "exit": close_payload.get("exit"),
                "initialStop": close_payload.get("initialStop"),
                "target": close_payload.get("target"),
                "originalNotional": close_payload.get("originalNotional"),
                "netPnl": close_payload.get("netPnl"),
                "grossPnl": close_payload.get("grossPnl"),
                "fees": close_payload.get("fees"),
                "maeUsd": close_payload.get("maeUsd"),
                "mfeUsd": close_payload.get("mfeUsd"),
                "maeR": close_payload.get("maeR"),
                "mfeR": close_payload.get("mfeR"),
                "reason": close_payload.get("reason"),
                "partialTaken": close_payload.get("partialTaken"),
                "timelineCount": len(timeline),
                "frameCount": len(frames),
            }
            reviews.append({
                "summary": summary,
                "plan": plan,
                "strategyDetails": (
                    close_payload.get("strategyDetails")
                    or plan.get("strategy_details")
                    or {}
                ),
                "openSnapshot": open_payload.get("market"),
                "closeSnapshot": close_payload.get("market"),
                "candles": candles,
                "frames": frames,
                "timeline": timeline,
            })
        return reviews

    def trade_review_summaries(
        self,
        name: str | None = None,
    ) -> list[dict]:
        path = self._session_path(name)
        reviews = self._build_trade_reviews(
            self._read_rows(path)
        )
        return [review["summary"] for review in reviews]

    def trade_review(
        self,
        review_id: str,
        name: str | None = None,
    ) -> dict:
        path = self._session_path(name)
        reviews = self._build_trade_reviews(
            self._read_rows(path)
        )
        for review in reviews:
            if review["summary"]["reviewId"] == review_id:
                return review
        raise KeyError(review_id)


    def opportunity_analysis(
        self,
        name: str | None = None,
        *,
        horizon_seconds: float = 120.0,
    ) -> dict:
        from .opportunity_review import analyze_session_rows

        path = self._session_path(name)
        return analyze_session_rows(
            self._read_rows(path),
            horizon_seconds=horizon_seconds,
        )


    def research_rows(
        self,
        name: str,
        symbol: str | None = None,
    ) -> list[dict]:
        path = self._safe_path(name)
        rows = self._read_rows(path)
        result: list[dict] = []
        for row in rows:
            if row.get("event") != "research_frame":
                continue
            if symbol is not None and row.get("symbol") != symbol:
                continue
            result.append(row)
        return result
