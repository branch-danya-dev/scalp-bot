from __future__ import annotations

import json
from collections import deque
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
        # Live trade-review cache. The UI must never re-read a multi-GB
        # research JSONL simply because a user opened a closed trade.
        self._live_review_pre_roll: dict[str, deque[dict]] = {}
        self._live_review_open: dict[
            tuple[str, str | None],
            list[dict],
        ] = {}
        self._live_reviews: list[dict] = []
        self._live_reviews_by_id: dict[str, dict] = {}

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
            self._capture_live_review_row(row)

    @staticmethod
    def _review_row_relevant(row: dict) -> bool:
        return row.get("event") in {
            "decision",
            "risk_reject",
            "setup_blocked",
            "entry_pending",
            "entry_cancelled",
            "trade_opened",
            "partial_take",
            "trade_closed",
            "setup_consumed",
            "setup_rearmed",
            "strategy_error",
            "research_frame",
            "market_frame",
        }

    def _capture_live_review_row(self, row: dict) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol or not self._review_row_relevant(row):
            return

        event = str(row.get("event") or "")
        ts = self._row_ts(row)
        pre_roll = self._live_review_pre_roll.setdefault(
            symbol,
            deque(),
        )

        if event == "trade_opened":
            setup_id = self._setup_id_from_open(
                row.get("payload") or {}
            )
            self._live_review_open[
                (symbol, setup_id)
            ] = [*pre_roll, row]
        else:
            for (open_symbol, _setup_id), rows in list(
                self._live_review_open.items()
            ):
                if open_symbol == symbol:
                    rows.append(row)

        if event == "trade_closed":
            setup_id = self._setup_id_from_close(
                row.get("payload") or {}
            )
            key = (symbol, setup_id)
            rows = self._live_review_open.pop(key, None)
            if rows is None:
                fallback = next(
                    (
                        candidate
                        for candidate in self._live_review_open
                        if candidate[0] == symbol
                    ),
                    None,
                )
                if fallback is not None:
                    rows = self._live_review_open.pop(fallback)
            if rows:
                reviews = self._build_trade_reviews(
                    rows,
                    pre_roll_seconds=120.0,
                    post_roll_seconds=0.0,
                )
                if reviews:
                    review = reviews[-1]
                    review_id = review["summary"]["reviewId"]
                    self._live_reviews.append(review)
                    self._live_reviews = self._live_reviews[-200:]
                    self._live_reviews_by_id[review_id] = review
                    live_ids = {
                        item["summary"]["reviewId"]
                        for item in self._live_reviews
                    }
                    self._live_reviews_by_id = {
                        key: value
                        for key, value
                        in self._live_reviews_by_id.items()
                        if key in live_ids
                    }

        pre_roll.append(row)
        cutoff = ts - 120.0
        while (
            pre_roll
            and self._row_ts(pre_roll[0]) < cutoff
        ):
            pre_roll.popleft()

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
            "entry_pending",
            "entry_cancelled",
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
                    if (
                        event == "decision"
                        and isinstance(payload, dict)
                        and not isinstance(payload.get("trace"), dict)
                    ):
                        from .observability import build_trace_from_public
                        payload = dict(payload)
                        payload["trace"] = build_trace_from_public(
                            payload,
                            int(ts * 1000),
                        )
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
                "entryFeeUsd": close_payload.get("entryFeeUsd"),
                "exitMovePct": close_payload.get("exitMovePct"),
                "exitMoveBps": close_payload.get("exitMoveBps"),
                "maxFavorableMovePct": close_payload.get("maxFavorableMovePct"),
                "maxFavorableMoveBps": close_payload.get("maxFavorableMoveBps"),
                "maxAdverseMovePct": close_payload.get("maxAdverseMovePct"),
                "maxAdverseMoveBps": close_payload.get("maxAdverseMoveBps"),
                "mfePrice": close_payload.get("mfePrice"),
                "maePrice": close_payload.get("maePrice"),
                "mfeAt": close_payload.get("mfeAt"),
                "maeAt": close_payload.get("maeAt"),
                "partialTakenAt": close_payload.get("partialTakenAt"),
                "plannedFirstTakeMovePct": close_payload.get("plannedFirstTakeMovePct"),
                "movementFloorBands": close_payload.get("movementFloorBands"),
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
        if name in {None, "", "current"}:
            return [
                review["summary"]
                for review in self._live_reviews
            ]
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
        if name in {None, "", "current"}:
            review = self._live_reviews_by_id.get(review_id)
            if review is None:
                raise KeyError(review_id)
            return review
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


    @staticmethod
    def _compact_market_snapshot(
        snapshot: dict | None,
        *,
        book_depth: int = 10,
    ) -> dict | None:
        if not isinstance(snapshot, dict):
            return None
        book = snapshot.get("orderbook") or {}
        return {
            "lastPrice": snapshot.get("lastPrice"),
            "trend": snapshot.get("trend"),
            "marketContext": snapshot.get("marketContext"),
            "orderbook": {
                "bids": list(book.get("bids") or [])[:book_depth],
                "asks": list(book.get("asks") or [])[:book_depth],
                "bestBid": book.get("bestBid"),
                "bestAsk": book.get("bestAsk"),
                "spreadPct": book.get("spreadPct"),
            },
            "bookHealth": snapshot.get("bookHealth"),
            "candleHealth": snapshot.get("candleHealth"),
            "tradeFlow": snapshot.get("tradeFlow"),
            "bookFlow": snapshot.get("bookFlow"),
            "densityContext": snapshot.get("densityContext"),
        }

    @staticmethod
    def _compact_closed_trade(row: dict) -> dict:
        payload = dict(row.get("payload") or {})
        payload.pop("market", None)
        return {
            "ts": SessionRecorder._row_ts(row),
            "symbol": row.get("symbol"),
            **payload,
        }

    @classmethod
    def _build_session_report(
        cls,
        path: Path,
        rows: list[dict],
        *,
        horizon_seconds: float = 120.0,
    ) -> dict:
        from .market_interaction_review import analyze_market_interactions
        from .opportunity_review import analyze_session_rows

        event_counts: dict[str, int] = {}
        symbols: set[str] = set()
        scanner_history: list[dict] = []
        activation_history: list[dict] = []
        closed_trades: list[dict] = []
        run_summary: dict | None = None
        chart_candles: dict[str, dict[int, dict]] = {}
        coverage: dict[str, dict] = {}
        strategy_diagnostics: dict[str, dict] = {}
        market_context_counts = {
            "legacyTrend": {},
            "htfBias": {},
            "localRegime": {},
            "flowDirection": {},
            "longFlowAlignment": {},
            "shortFlowAlignment": {},
            "liquidityState": {},
            "liquidityBias": {},
            "executionReady": {},
            "structureAvailable": {},
        }

        def strategy_diag(strategy: str) -> dict:
            return strategy_diagnostics.setdefault(
                strategy,
                {
                    "decisionUpdates": 0,
                    "tradeableDecisionUpdates": 0,
                    "waitDecisionUpdates": 0,
                    "tradeableSetupKeys": set(),
                    "riskRejectUpdates": 0,
                    "riskRejectedSetupKeys": set(),
                    "stateCounts": {},
                    "riskRejectReasons": {},
                    "playbookContextSourceCounts": {},
                    "playbookDirectionCounts": {},
                    "entryContextBlockedUpdates": 0,
                    "entryContextBlockerCounts": {},
                    "arbiterBlockedUpdates": 0,
                    "arbiterBlockerCounts": {},
                    "arbiterConflictCounts": {},
                    "selectedConfluenceCounts": {},
                    "entryPending": 0,
                    "entryCancelled": 0,
                    "tradesOpened": 0,
                    "entryFreshnessCounts": {},
                    "entryMoveSpentTotal": 0.0,
                    "entryMoveSpentSamples": 0,
                    "entryFlowAlignmentCounts": {},
                    "entryFlowAlignmentBySide": {
                        "long": {},
                        "short": {},
                    },
                    "entryFlowScoreTotal": 0.0,
                    "entryFlowScoreSamples": 0,
                    "entryLiquidityAlignmentCounts": {},
                    "entryLiquidityAlignmentBySide": {
                        "long": {},
                        "short": {},
                    },
                    "entryLiquidityScoreTotal": 0.0,
                    "entryLiquidityScoreSamples": 0,
                    "entryLocalRegimeCounts": {},
                    "entryHtfBiasCounts": {},
                    "entryExecutionReadyCounts": {},
                    "tradesClosed": 0,
                    "wins": 0,
                    "losses": 0,
                    "grossPnl": 0.0,
                    "fees": 0.0,
                    "netPnl": 0.0,
                },
            )

        def setup_key(
            symbol: str,
            strategy: str,
            payload: dict,
        ) -> str:
            setup_id = (
                payload.get("setup_id")
                or payload.get("setupId")
            )
            if setup_id:
                return f"{symbol}:{setup_id}"
            return (
                f"{symbol}:{strategy}:"
                f"{payload.get('entry')}:"
                f"{payload.get('stop')}:"
                f"{payload.get('target')}"
            )

        def symbol_coverage(symbol: str) -> dict:
            return coverage.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "firstTs": None,
                    "lastTs": None,
                    "marketFrames": 0,
                    "researchFrames": 0,
                    "orderbookFrames": 0,
                    "maxBidDepth": 0,
                    "maxAskDepth": 0,
                    "tradeSnapshots": 0,
                    "bootstrapCandles": 0,
                    "chartTimeframes": [],
                },
            )

        def remember_candle(symbol: str, candle: dict | None) -> None:
            if not isinstance(candle, dict):
                return
            raw_time = candle.get("time")
            if raw_time is None:
                return
            chart_candles.setdefault(symbol, {})[int(raw_time)] = candle

        for row in rows:
            event = str(row.get("event") or "")
            event_counts[event] = event_counts.get(event, 0) + 1
            ts = cls._row_ts(row)
            payload = row.get("payload") or {}
            symbol = str(row.get("symbol") or "")

            if event == "decision":
                strategy = str(payload.get("strategy") or "")
                if strategy:
                    item_diag = strategy_diag(strategy)
                    item_diag["decisionUpdates"] += 1
                    action = str(payload.get("action") or "")
                    details = payload.get("details") or {}
                    trace = payload.get("trace") or {}
                    state = str(
                        details.get("state")
                        or trace.get("state")
                        or "unknown"
                    )
                    states = item_diag["stateCounts"]
                    states[state] = states.get(state, 0) + 1
                    playbook_context = (
                        details.get("playbookContext")
                        if isinstance(details, dict)
                        else None
                    )
                    if isinstance(playbook_context, dict):
                        source = str(
                            playbook_context.get("source")
                            or "unknown"
                        )
                        source_counts = item_diag[
                            "playbookContextSourceCounts"
                        ]
                        source_counts[source] = (
                            source_counts.get(source, 0) + 1
                        )
                        direction = str(
                            playbook_context.get(
                                "primaryDirection"
                            )
                            or "flat"
                        )
                        direction_counts = item_diag[
                            "playbookDirectionCounts"
                        ]
                        direction_counts[direction] = (
                            direction_counts.get(direction, 0) + 1
                        )

                    entry_context = (
                        details.get("entryContextAssessment")
                        if isinstance(details, dict)
                        else None
                    )
                    if (
                        isinstance(entry_context, dict)
                        and entry_context.get("allowed") is False
                    ):
                        item_diag["entryContextBlockedUpdates"] += 1
                        blocker_counts = item_diag[
                            "entryContextBlockerCounts"
                        ]
                        for blocker in (
                            entry_context.get("blockers") or []
                        ):
                            key_blocker = str(blocker)
                            blocker_counts[key_blocker] = (
                                blocker_counts.get(
                                    key_blocker,
                                    0,
                                )
                                + 1
                            )

                    if action in {"long", "short"}:
                        item_diag["tradeableDecisionUpdates"] += 1
                        item_diag["tradeableSetupKeys"].add(
                            setup_key(symbol, strategy, payload)
                        )
                    else:
                        item_diag["waitDecisionUpdates"] += 1

            if event == "risk_reject":
                strategy = str(payload.get("strategy") or "")
                if strategy:
                    item_diag = strategy_diag(strategy)
                    item_diag["riskRejectUpdates"] += 1
                    decision = payload.get("decision") or {}
                    item_diag["riskRejectedSetupKeys"].add(
                        setup_key(symbol, strategy, decision)
                    )
                    reason = str(payload.get("reason") or "unknown")
                    reasons = item_diag["riskRejectReasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1

            if event == "arbiter_blocked":
                strategy = str(payload.get("strategy") or "")
                if strategy:
                    item_diag = strategy_diag(strategy)
                    item_diag["arbiterBlockedUpdates"] += 1
                    blocker_counts = item_diag["arbiterBlockerCounts"]
                    for blocker in payload.get("blockers") or []:
                        key_blocker = str(blocker)
                        blocker_counts[key_blocker] = (
                            blocker_counts.get(key_blocker, 0) + 1
                        )
                    conflict_counts = item_diag["arbiterConflictCounts"]
                    for peer in payload.get(
                        "conflictingStrategies"
                    ) or []:
                        peer_key = str(peer)
                        conflict_counts[peer_key] = (
                            conflict_counts.get(peer_key, 0) + 1
                        )

            if event in {"entry_pending", "entry_cancelled"}:
                strategy = str(
                    payload.get("strategy")
                    or (payload.get("plan") or {}).get("strategy")
                    or ""
                )
                if strategy:
                    key = (
                        "entryPending"
                        if event == "entry_pending"
                        else "entryCancelled"
                    )
                    strategy_diag(strategy)[key] += 1

            if event == "trade_opened":
                plan = payload.get("plan") or {}
                strategy = str(
                    plan.get("strategy")
                    or payload.get("strategy")
                    or ""
                )
                if strategy:
                    item_diag = strategy_diag(strategy)
                    item_diag["tradesOpened"] += 1
                    strategy_details = plan.get("strategy_details") or {}
                    freshness = (
                        strategy_details.get("entryFreshness")
                        if isinstance(strategy_details, dict)
                        else None
                    )
                    if isinstance(freshness, dict):
                        freshness_class = str(
                            freshness.get("classification")
                            or "unknown"
                        )
                        counts = item_diag["entryFreshnessCounts"]
                        counts[freshness_class] = (
                            counts.get(freshness_class, 0) + 1
                        )
                        spent = freshness.get("moveSpentRatio")
                        if isinstance(spent, (int, float)):
                            item_diag["entryMoveSpentTotal"] += float(spent)
                            item_diag["entryMoveSpentSamples"] += 1

                    flow_alignment = (
                        strategy_details.get("flowAlignment")
                        if isinstance(strategy_details, dict)
                        else None
                    )
                    if isinstance(flow_alignment, dict):
                        flow_class = str(
                            flow_alignment.get("classification")
                            or "unknown"
                        )
                        flow_counts = item_diag[
                            "entryFlowAlignmentCounts"
                        ]
                        flow_counts[flow_class] = (
                            flow_counts.get(flow_class, 0) + 1
                        )
                        side = str(plan.get("side") or "")
                        side_counts = item_diag[
                            "entryFlowAlignmentBySide"
                        ].get(side)
                        if isinstance(side_counts, dict):
                            side_counts[flow_class] = (
                                side_counts.get(flow_class, 0) + 1
                            )
                        flow_score = flow_alignment.get("score")
                        if isinstance(flow_score, (int, float)):
                            item_diag["entryFlowScoreTotal"] += float(
                                flow_score
                            )
                            item_diag["entryFlowScoreSamples"] += 1

                    arbitration = (
                        payload.get("semanticArbitration")
                        or (
                            strategy_details.get(
                                "semanticArbitration"
                            )
                            if isinstance(strategy_details, dict)
                            else None
                        )
                    )
                    if isinstance(arbitration, dict):
                        confluence = int(
                            arbitration.get("confluenceCount")
                            or 0
                        )
                        confluence_counts = item_diag[
                            "selectedConfluenceCounts"
                        ]
                        confluence_key = str(confluence)
                        confluence_counts[confluence_key] = (
                            confluence_counts.get(
                                confluence_key,
                                0,
                            )
                            + 1
                        )

                    decision_context = (
                        strategy_details.get("decisionContext")
                        if isinstance(strategy_details, dict)
                        else None
                    )
                    if isinstance(decision_context, dict):
                        for target_key, source_key in (
                            ("entryLocalRegimeCounts", "localRegime"),
                            ("entryHtfBiasCounts", "htfBias"),
                        ):
                            value = str(
                                decision_context.get(source_key)
                                or "unknown"
                            )
                            counts = item_diag[target_key]
                            counts[value] = counts.get(value, 0) + 1
                        ready_key = str(
                            bool(
                                decision_context.get(
                                    "executionReady"
                                )
                            )
                        ).lower()
                        ready_counts = item_diag[
                            "entryExecutionReadyCounts"
                        ]
                        ready_counts[ready_key] = (
                            ready_counts.get(ready_key, 0) + 1
                        )

                    liquidity_alignment = (
                        strategy_details.get("liquidityAlignment")
                        if isinstance(strategy_details, dict)
                        else None
                    )
                    if isinstance(liquidity_alignment, dict):
                        liquidity_class = str(
                            liquidity_alignment.get("classification")
                            or "unknown"
                        )
                        liquidity_counts = item_diag[
                            "entryLiquidityAlignmentCounts"
                        ]
                        liquidity_counts[liquidity_class] = (
                            liquidity_counts.get(liquidity_class, 0) + 1
                        )
                        side = str(plan.get("side") or "")
                        side_counts = item_diag[
                            "entryLiquidityAlignmentBySide"
                        ].get(side)
                        if isinstance(side_counts, dict):
                            side_counts[liquidity_class] = (
                                side_counts.get(liquidity_class, 0) + 1
                            )
                        liquidity_score = liquidity_alignment.get("score")
                        if isinstance(liquidity_score, (int, float)):
                            item_diag["entryLiquidityScoreTotal"] += float(
                                liquidity_score
                            )
                            item_diag["entryLiquidityScoreSamples"] += 1

            if event == "trade_closed":
                strategy = str(payload.get("strategy") or "")
                if strategy:
                    item_diag = strategy_diag(strategy)
                    net = float(payload.get("netPnl") or 0.0)
                    item_diag["tradesClosed"] += 1
                    item_diag["grossPnl"] += float(
                        payload.get("grossPnl") or 0.0
                    )
                    item_diag["fees"] += float(
                        payload.get("fees") or 0.0
                    )
                    item_diag["netPnl"] += net
                    if net > 0:
                        item_diag["wins"] += 1
                    elif net < 0:
                        item_diag["losses"] += 1

            if event == "run_summary":
                run_summary = dict(payload)

            if event == "scanner_update":
                scanner_history.append({
                    "ts": ts,
                    "active": list(payload.get("active") or []),
                    "promotedFromTop": list(payload.get("promotedFromTop") or []),
                    "ranked": list(payload.get("ranked") or []),
                })

            if event in {"symbol_activated", "symbol_deactivated"}:
                activation_history.append({
                    "ts": ts,
                    "event": event,
                    "symbol": symbol or None,
                    "reason": payload.get("reason"),
                })

            if event == "trade_closed":
                closed_trades.append(cls._compact_closed_trade(row))

            if not symbol:
                continue
            symbols.add(symbol)
            item = symbol_coverage(symbol)
            item["firstTs"] = ts if item["firstTs"] is None else min(item["firstTs"], ts)
            item["lastTs"] = ts if item["lastTs"] is None else max(item["lastTs"], ts)

            if event == "symbol_activated":
                market = payload.get("market") or {}
                candles = market.get("candles") or []
                item["bootstrapCandles"] = max(item["bootstrapCandles"], len(candles))
                for candle in candles:
                    remember_candle(symbol, candle)
                chart_series = market.get("chartSeries") or {}
                if isinstance(chart_series, dict):
                    item["chartTimeframes"] = sorted(chart_series)

            if event in {"market_frame", "research_frame"}:
                key = "marketFrames" if event == "market_frame" else "researchFrames"
                item[key] += 1
                remember_candle(symbol, payload.get("candle"))
                market_context = payload.get("marketContext") or {}
                if isinstance(market_context, dict):
                    legacy = str(
                        market_context.get("legacyTrend")
                        or payload.get("trend")
                        or "unknown"
                    )
                    htf = market_context.get("htfBias") or {}
                    local = market_context.get("localRegime") or {}
                    flow_context = market_context.get("flowContext") or {}
                    htf_key = (
                        str(htf.get("bias") or "unknown")
                        if isinstance(htf, dict)
                        else "unknown"
                    )
                    local_key = (
                        str(local.get("regime") or "unknown")
                        if isinstance(local, dict)
                        else "unknown"
                    )
                    flow_direction = (
                        str(flow_context.get("dominantDirection") or "unknown")
                        if isinstance(flow_context, dict)
                        else "unknown"
                    )
                    liquidity = (
                        market_context.get("liquidityEvidence") or {}
                        if isinstance(market_context, dict)
                        else {}
                    )
                    liquidity_state = (
                        str(liquidity.get("state") or "unknown")
                        if isinstance(liquidity, dict)
                        else "unknown"
                    )
                    liquidity_bias = (
                        str(liquidity.get("directionalBias") or "unknown")
                        if isinstance(liquidity, dict)
                        else "unknown"
                    )
                    long_alignment = (
                        flow_context.get("longAlignment") or {}
                        if isinstance(flow_context, dict)
                        else {}
                    )
                    short_alignment = (
                        flow_context.get("shortAlignment") or {}
                        if isinstance(flow_context, dict)
                        else {}
                    )
                    long_alignment_key = (
                        str(long_alignment.get("classification") or "unknown")
                        if isinstance(long_alignment, dict)
                        else "unknown"
                    )
                    short_alignment_key = (
                        str(short_alignment.get("classification") or "unknown")
                        if isinstance(short_alignment, dict)
                        else "unknown"
                    )
                    execution_context = (
                        market_context.get("executionContext") or {}
                        if isinstance(market_context, dict)
                        else {}
                    )
                    structure_context = (
                        market_context.get("structureContext")
                        if isinstance(market_context, dict)
                        else None
                    )
                    execution_ready_key = (
                        str(bool(execution_context.get("ready"))).lower()
                        if isinstance(execution_context, dict)
                        else "unknown"
                    )
                    structure_available_key = (
                        "true"
                        if isinstance(structure_context, dict)
                        else "false"
                    )
                    for bucket_name, value in (
                        ("legacyTrend", legacy),
                        ("htfBias", htf_key),
                        ("localRegime", local_key),
                        ("flowDirection", flow_direction),
                        ("longFlowAlignment", long_alignment_key),
                        ("shortFlowAlignment", short_alignment_key),
                        ("liquidityState", liquidity_state),
                        ("liquidityBias", liquidity_bias),
                        ("executionReady", execution_ready_key),
                        ("structureAvailable", structure_available_key),
                    ):
                        bucket = market_context_counts[bucket_name]
                        bucket[value] = bucket.get(value, 0) + 1
                book = payload.get("orderbook") or {}
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if bids or asks:
                    item["orderbookFrames"] += 1
                    item["maxBidDepth"] = max(item["maxBidDepth"], len(bids))
                    item["maxAskDepth"] = max(item["maxAskDepth"], len(asks))

            market = payload.get("market")
            if event in {"trade_opened", "trade_closed", "risk_reject", "partial_take"} and isinstance(market, dict):
                item["tradeSnapshots"] += 1
                for candle in market.get("candles") or []:
                    remember_candle(symbol, candle)

        trade_reviews = cls._build_trade_reviews(rows)
        compact_reviews = [
            {
                "summary": review["summary"],
                "plan": review["plan"],
                "strategyDetails": review["strategyDetails"],
                "candles": review["candles"],
                "timeline": review["timeline"],
                "openSnapshot": cls._compact_market_snapshot(
                    review.get("openSnapshot")
                ),
                "closeSnapshot": cls._compact_market_snapshot(
                    review.get("closeSnapshot")
                ),
            }
            for review in trade_reviews
        ]
        opportunity = analyze_session_rows(
            rows,
            horizon_seconds=horizon_seconds,
        )
        market_interactions = analyze_market_interactions(rows)
        latest_scanner = scanner_history[-1] if scanner_history else {}

        market_symbols = {}
        for symbol in sorted(symbols):
            candle_map = chart_candles.get(symbol, {})
            market_symbols[symbol] = {
                "coverage": symbol_coverage(symbol),
                "chartCandles": [candle_map[key] for key in sorted(candle_map)],
            }

        strategy_report = {}
        for strategy, item in strategy_diagnostics.items():
            tradeable_keys = item.pop("tradeableSetupKeys")
            rejected_keys = item.pop("riskRejectedSetupKeys")
            move_spent_total = float(item.pop("entryMoveSpentTotal"))
            move_spent_samples = int(item.pop("entryMoveSpentSamples"))
            flow_score_total = float(item.pop("entryFlowScoreTotal"))
            flow_score_samples = int(item.pop("entryFlowScoreSamples"))
            liquidity_score_total = float(
                item.pop("entryLiquidityScoreTotal")
            )
            liquidity_score_samples = int(
                item.pop("entryLiquidityScoreSamples")
            )
            strategy_report[strategy] = {
                **item,
                "averageEntryLiquidityAlignmentScore": (
                    liquidity_score_total / liquidity_score_samples
                    if liquidity_score_samples > 0
                    else None
                ),
                "entryLiquidityScoreSamples": liquidity_score_samples,
                "averageEntryFlowAlignmentScore": (
                    flow_score_total / flow_score_samples
                    if flow_score_samples > 0
                    else None
                ),
                "entryFlowScoreSamples": flow_score_samples,
                "averageEntryMoveSpentRatio": (
                    move_spent_total / move_spent_samples
                    if move_spent_samples > 0
                    else None
                ),
                "entryMoveSpentSamples": move_spent_samples,
                "uniqueTradeableSetups": len(tradeable_keys),
                "uniqueRiskRejectedSetups": len(rejected_keys),
                "tradeableSetupKeys": sorted(tradeable_keys),
                "riskRejectedSetupKeys": sorted(rejected_keys),
            }

        return {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC).isoformat(),
            "session": {
                "file": path.name,
                "sizeBytes": path.stat().st_size if path.exists() else 0,
                "eventCount": len(rows),
                "eventCounts": event_counts,
            },
            "runSummary": run_summary,
            "postRunOpportunity": opportunity,
            "marketInteractionResearch": market_interactions,
            "strategyDiagnostics": strategy_report,
            "marketContextDiagnostics": {
                "frameCounts": market_context_counts,
            },
            "closedTrades": closed_trades,
            "tradeReviews": compact_reviews,
            "coins": {
                "seenSymbols": sorted(symbols),
                "scannerUpdateCount": len(scanner_history),
                "latestRanked": latest_scanner.get("ranked", []),
                "activeAtLastScan": latest_scanner.get("active", []),
                "scannerHistory": scanner_history,
                "activationHistory": activation_history,
            },
            "marketData": {
                "rawSessionFile": path.name,
                "rawSessionContainsFullFrames": True,
                "orderbooksStoredInRawFrames": any(
                    item["orderbookFrames"] > 0
                    for item in coverage.values()
                ),
                "chartCandlesStoredInReport": True,
                "symbols": market_symbols,
            },
        }

    @classmethod
    def report_for_file(
        cls,
        session_path: str | Path,
        *,
        horizon_seconds: float = 120.0,
    ) -> dict:
        path = Path(session_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return cls._build_session_report(
            path,
            cls._read_rows(path),
            horizon_seconds=horizon_seconds,
        )

    @classmethod
    def write_report_for_file(
        cls,
        session_path: str | Path,
        *,
        horizon_seconds: float = 120.0,
    ) -> Path:
        path = Path(session_path)
        report = cls.report_for_file(
            path,
            horizon_seconds=horizon_seconds,
        )
        output = path.with_name(f"{path.stem}-report.json")
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output

    def session_report(
        self,
        name: str | None = None,
        *,
        horizon_seconds: float = 120.0,
    ) -> dict:
        path = self._session_path(name)
        return self.report_for_file(
            path,
            horizon_seconds=horizon_seconds,
        )

    def write_session_report(
        self,
        name: str | None = None,
        *,
        horizon_seconds: float = 120.0,
    ) -> Path:
        path = self._session_path(name)
        return self.write_report_for_file(
            path,
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
