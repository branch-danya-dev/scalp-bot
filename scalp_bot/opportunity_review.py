from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _observation(row: dict) -> dict | None:
    payload = row.get("payload") or {}
    candle = payload.get("candle")
    if isinstance(candle, dict):
        close = candle.get("close")
        if close is None:
            close = payload.get("lastPrice")
        if close is None:
            return None
        return {
            "ts": _row_ts(row),
            "high": float(candle.get("high") or close),
            "low": float(candle.get("low") or close),
            "close": float(close),
        }
    market = payload.get("market")
    if isinstance(market, dict):
        candles = market.get("candles") or []
        if candles:
            candle = candles[-1]
            close = candle.get("close")
            if close is not None:
                return {
                    "ts": _row_ts(row),
                    "high": float(candle.get("high") or close),
                    "low": float(candle.get("low") or close),
                    "close": float(close),
                }
    return None


def _observations_by_symbol(rows: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("event") not in {"research_frame", "market_frame"}:
            continue
        symbol = row.get("symbol")
        if not symbol:
            continue
        observation = _observation(row)
        if observation is not None:
            result[str(symbol)].append(observation)
    for values in result.values():
        values.sort(key=lambda item: item["ts"])
    return result


def _future_path(
    observations: list[dict],
    start_ts: float,
    horizon_seconds: float,
) -> list[dict]:
    end = start_ts + horizon_seconds
    return [
        row
        for row in observations
        if start_ts < row["ts"] <= end
    ]


def _hypothetical_outcome(
    decision: dict,
    future: list[dict],
) -> dict:
    action = str(decision.get("action") or "")
    entry = decision.get("entry")
    stop = decision.get("stop")
    target = decision.get("target")
    if (
        action not in {"long", "short"}
        or entry is None
        or stop is None
        or target is None
    ):
        return {
            "outcome": "not_simulatable",
            "mfeR": None,
            "maeR": None,
            "secondsToTarget": None,
            "secondsToStop": None,
        }

    entry = float(entry)
    stop = float(stop)
    target = float(target)
    risk = abs(entry - stop)
    if risk <= 0:
        return {
            "outcome": "not_simulatable",
            "mfeR": None,
            "maeR": None,
            "secondsToTarget": None,
            "secondsToStop": None,
        }

    mfe = 0.0
    mae = 0.0
    first: str | None = None
    target_ts = stop_ts = None
    start_ts = future[0]["ts"] if future else None
    for row in future:
        if action == "long":
            favorable = row["high"] - entry
            adverse = entry - row["low"]
            target_hit = row["high"] >= target
            stop_hit = row["low"] <= stop
        else:
            favorable = entry - row["low"]
            adverse = row["high"] - entry
            target_hit = row["low"] <= target
            stop_hit = row["high"] >= stop
        mfe = max(mfe, favorable)
        mae = max(mae, adverse)

        if target_hit and target_ts is None:
            target_ts = row["ts"]
        if stop_hit and stop_ts is None:
            stop_ts = row["ts"]
        if first is None:
            if target_hit and stop_hit:
                first = "ambiguous_same_frame"
            elif target_hit:
                first = "target_first"
            elif stop_hit:
                first = "stop_first"

    outcome = first or "unresolved"
    base = start_ts if start_ts is not None else 0.0
    return {
        "outcome": outcome,
        "mfeR": mfe / risk,
        "maeR": mae / risk,
        "secondsToTarget": (
            target_ts - base
            if target_ts is not None and start_ts is not None
            else None
        ),
        "secondsToStop": (
            stop_ts - base
            if stop_ts is not None and start_ts is not None
            else None
        ),
    }


def analyze_session_rows(
    rows: list[dict],
    *,
    horizon_seconds: float = 120.0,
) -> dict[str, Any]:
    observations = _observations_by_symbol(rows)
    candidates: list[dict] = []
    early_exits: list[dict] = []
    latest_decisions: dict[tuple[str, str], dict] = {}

    for index, row in enumerate(rows):
        event = row.get("event")
        payload = row.get("payload") or {}
        symbol = str(row.get("symbol") or "")
        ts = _row_ts(row)

        if event == "decision":
            strategy = str(payload.get("strategy") or "")
            if strategy:
                latest_decisions[(symbol, strategy)] = payload

        if event in {"risk_reject", "setup_blocked"}:
            strategy = str(payload.get("strategy") or "")
            decision = payload.get("decision")
            if not isinstance(decision, dict) and strategy:
                decision = latest_decisions.get(
                    (symbol, strategy)
                )
            if not isinstance(decision, dict):
                continue
            future = _future_path(
                observations.get(symbol, []),
                ts,
                horizon_seconds,
            )
            outcome = _hypothetical_outcome(decision, future)
            review_class = {
                "target_first": "missed_target_first",
                "stop_first": "correct_reject_candidate",
                "ambiguous_same_frame": "ambiguous",
                "unresolved": "unresolved",
            }.get(outcome["outcome"], "not_simulatable")
            candidates.append({
                "reviewId": f"reject-{index}",
                "ts": ts,
                "symbol": symbol,
                "strategy": (
                    decision.get("strategy")
                    or payload.get("strategy")
                ),
                "sourceEvent": event,
                "reason": payload.get("reason"),
                "side": decision.get("action"),
                "entry": decision.get("entry"),
                "stop": decision.get("stop"),
                "target": decision.get("target"),
                "classification": review_class,
                **outcome,
            })

        if event == "trade_closed":
            reason = str(payload.get("reason") or "")
            if reason in {"target", "runner_target"}:
                continue
            side = str(payload.get("side") or "")
            exit_price = payload.get("exit")
            target = payload.get("target")
            initial_stop = payload.get("initialStop")
            entry = payload.get("entry")
            if (
                side not in {"long", "short"}
                or exit_price is None
                or target is None
                or initial_stop is None
                or entry is None
            ):
                continue
            future = _future_path(
                observations.get(symbol, []),
                ts,
                horizon_seconds,
            )
            risk = abs(float(entry) - float(initial_stop))
            target_after = False
            post_mfe = 0.0
            for obs in future:
                if side == "long":
                    post_mfe = max(
                        post_mfe,
                        obs["high"] - float(exit_price),
                    )
                    target_after = target_after or (
                        obs["high"] >= float(target)
                    )
                else:
                    post_mfe = max(
                        post_mfe,
                        float(exit_price) - obs["low"],
                    )
                    target_after = target_after or (
                        obs["low"] <= float(target)
                    )
            early_exits.append({
                "reviewId": f"exit-{index}",
                "ts": ts,
                "symbol": symbol,
                "strategy": payload.get("strategy"),
                "side": side,
                "reason": reason,
                "exit": float(exit_price),
                "target": float(target),
                "netPnl": payload.get("netPnl"),
                "targetHitAfterExit": target_after,
                "postExitMfeR": (
                    post_mfe / risk
                    if risk > 0
                    else None
                ),
                "classification": (
                    "early_exit_review"
                    if target_after
                    else "exit_supported_by_followup"
                ),
            })

    by_reason = Counter(
        str(row.get("reason") or "unknown")
        for row in candidates
    )
    by_strategy: dict[str, dict[str, int]] = {}
    for row in candidates:
        key = str(row.get("strategy") or "unknown")
        bucket = by_strategy.setdefault(
            key,
            {
                "total": 0,
                "missedTargetFirst": 0,
                "correctRejectCandidates": 0,
                "ambiguous": 0,
            },
        )
        bucket["total"] += 1
        if row["classification"] == "missed_target_first":
            bucket["missedTargetFirst"] += 1
        elif row["classification"] == "correct_reject_candidate":
            bucket["correctRejectCandidates"] += 1
        elif row["classification"] == "ambiguous":
            bucket["ambiguous"] += 1

    return {
        "horizonSeconds": horizon_seconds,
        "summary": {
            "rejectedCandidates": len(candidates),
            "missedTargetFirst": sum(
                row["classification"] == "missed_target_first"
                for row in candidates
            ),
            "correctRejectCandidates": sum(
                row["classification"] == "correct_reject_candidate"
                for row in candidates
            ),
            "ambiguous": sum(
                row["classification"] == "ambiguous"
                for row in candidates
            ),
            "earlyExitReviews": sum(
                row["classification"] == "early_exit_review"
                for row in early_exits
            ),
        },
        "byReason": dict(by_reason),
        "byStrategy": by_strategy,
        "candidates": candidates,
        "earlyExits": early_exits,
    }
