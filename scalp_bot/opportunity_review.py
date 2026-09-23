from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .hindsight_review import analyze_hindsight_opportunities
from .market_interaction_review import TRACKED_STATES


DEFAULT_MARKET_MOVE_PCT = 0.002
DEFAULT_MARKET_MOVE_HORIZON_SECONDS = 300.0
DEFAULT_MARKET_MOVE_DECISION_LOOKBACK_SECONDS = 30.0


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _observation(row: dict) -> dict | None:
    payload = row.get("payload") or {}
    direct_price = payload.get("lastPrice")
    candle = payload.get("candle")
    if isinstance(candle, dict):
        close = candle.get("close")
        if close is None:
            close = direct_price
        if close is None:
            return None
        sampled_price = (
            float(direct_price)
            if isinstance(direct_price, (int, float)) and direct_price > 0
            else float(close)
        )
        return {
            "ts": _row_ts(row),
            "high": float(candle.get("high") or close),
            "low": float(candle.get("low") or close),
            "close": float(close),
            "price": sampled_price,
        }
    market = payload.get("market")
    if isinstance(market, dict):
        market_price = market.get("lastPrice")
        candles = market.get("candles") or []
        if candles:
            candle = candles[-1]
            close = candle.get("close")
            if close is not None:
                sampled_price = (
                    float(market_price)
                    if isinstance(market_price, (int, float)) and market_price > 0
                    else float(close)
                )
                return {
                    "ts": _row_ts(row),
                    "high": float(candle.get("high") or close),
                    "low": float(candle.get("low") or close),
                    "close": float(close),
                    "price": sampled_price,
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


def _decision_state(decision: dict) -> str:
    details = decision.get("details") or {}
    trace = decision.get("trace") or {}
    return str(details.get("state") or trace.get("state") or "")


def _decision_hypothesis_side(decision: dict) -> str | None:
    action = str(decision.get("action") or "")
    if action in {"long", "short"}:
        return action

    strategy = str(decision.get("strategy") or "")
    details = decision.get("details") or {}
    trace = decision.get("trace") or {}
    obj = trace.get("object") or {}
    if not isinstance(obj, dict):
        obj = {}

    if strategy == "trend_structure":
        trend = str(trace.get("trend") or details.get("trend") or "")
        if trend == "up":
            return "long"
        if trend == "down":
            return "short"
        return None

    zone = details.get("zone") or {}
    if not isinstance(zone, dict):
        zone = {}
    label = " ".join(
        str(value or "")
        for value in (
            obj.get("label"),
            obj.get("kind"),
            zone.get("kind"),
        )
    ).lower()

    if strategy == "level_breakout":
        if "resistance" in label:
            return "long"
        if "support" in label:
            return "short"
    elif strategy == "weak_level_rejection":
        if "resistance" in label:
            return "short"
        if "support" in label:
            return "long"
    elif strategy == "orderbook_density":
        wall_side = str(details.get("wallSide") or obj.get("side") or "")
        if wall_side == "ask":
            return "short"
        if wall_side == "bid":
            return "long"
    return None


def _move_event(
    observations: list[dict],
    *,
    side: str,
    start_index: int,
    threshold_index: int,
    extreme_index: int,
    minimum_move_pct: float,
) -> dict:
    start = observations[start_index]
    threshold = observations[threshold_index]
    extreme = observations[extreme_index]
    start_price = float(start["price"])
    extreme_price = float(extreme["price"])
    if side == "long":
        move_pct = (extreme_price - start_price) / start_price
    else:
        move_pct = (start_price - extreme_price) / start_price
    return {
        "side": side,
        "startTs": start["ts"],
        "thresholdTs": threshold["ts"],
        "extremeTs": extreme["ts"],
        "startPrice": start_price,
        "thresholdPrice": float(threshold["price"]),
        "extremePrice": extreme_price,
        "minimumMovePct": minimum_move_pct,
        "maxMovePct": max(0.0, move_pct),
        "secondsToThreshold": max(0.0, threshold["ts"] - start["ts"]),
        "secondsToExtreme": max(0.0, extreme["ts"] - start["ts"]),
    }


def _significant_market_moves(
    observations: list[dict],
    *,
    minimum_move_pct: float,
    maximum_threshold_seconds: float,
) -> list[dict]:
    if len(observations) < 2 or minimum_move_pct <= 0:
        return []

    events: list[dict] = []
    direction: str | None = None

    high_index = low_index = 0
    high_price = low_price = float(observations[0]["price"])

    pivot_index = extreme_index = threshold_index = 0
    pivot_price = extreme_price = float(observations[0]["price"])

    for index in range(1, len(observations)):
        price = float(observations[index]["price"])

        if direction is None:
            if price >= high_price:
                high_price = price
                high_index = index
            if price <= low_price:
                low_price = price
                low_index = index

            up_move = (
                (price - low_price) / low_price
                if low_price > 0
                else 0.0
            )
            down_move = (
                (high_price - price) / high_price
                if high_price > 0
                else 0.0
            )
            if up_move >= minimum_move_pct and low_index < index:
                direction = "long"
                pivot_index = low_index
                pivot_price = low_price
                extreme_index = threshold_index = index
                extreme_price = price
            elif down_move >= minimum_move_pct and high_index < index:
                direction = "short"
                pivot_index = high_index
                pivot_price = high_price
                extreme_index = threshold_index = index
                extreme_price = price
            continue

        if direction == "long":
            if price >= extreme_price:
                extreme_price = price
                extreme_index = index
            reversal = (
                (extreme_price - price) / extreme_price
                if extreme_price > 0
                else 0.0
            )
            if reversal < minimum_move_pct:
                continue

            event = _move_event(
                observations,
                side="long",
                start_index=pivot_index,
                threshold_index=threshold_index,
                extreme_index=extreme_index,
                minimum_move_pct=minimum_move_pct,
            )
            if event["secondsToThreshold"] <= maximum_threshold_seconds:
                events.append(event)

            direction = "short"
            pivot_index = extreme_index
            pivot_price = extreme_price
            extreme_index = threshold_index = index
            extreme_price = price
            continue

        if price <= extreme_price:
            extreme_price = price
            extreme_index = index
        reversal = (
            (price - extreme_price) / extreme_price
            if extreme_price > 0
            else 0.0
        )
        if reversal < minimum_move_pct:
            continue

        event = _move_event(
            observations,
            side="short",
            start_index=pivot_index,
            threshold_index=threshold_index,
            extreme_index=extreme_index,
            minimum_move_pct=minimum_move_pct,
        )
        if event["secondsToThreshold"] <= maximum_threshold_seconds:
            events.append(event)

        direction = "long"
        pivot_index = extreme_index
        pivot_price = extreme_price
        extreme_index = threshold_index = index
        extreme_price = price

    if direction is not None:
        event = _move_event(
            observations,
            side=direction,
            start_index=pivot_index,
            threshold_index=threshold_index,
            extreme_index=extreme_index,
            minimum_move_pct=minimum_move_pct,
        )
        if (
            event["maxMovePct"] >= minimum_move_pct
            and event["secondsToThreshold"] <= maximum_threshold_seconds
        ):
            events.append(event)

    return events


def _latest_decisions_before(
    rows: list[dict],
    *,
    symbol: str,
    end_ts: float,
) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("symbol") != symbol:
            continue
        ts = _row_ts(row)
        if ts > end_ts:
            break
        event = str(row.get("event") or "")
        if event in {"symbol_activated", "symbol_deactivated"}:
            latest.clear()
            continue
        if event != "decision":
            continue
        payload = row.get("payload") or {}
        strategy = str(payload.get("strategy") or "")
        if strategy:
            latest[strategy] = {
                "ts": ts,
                "ageSeconds": max(0.0, end_ts - ts),
                "strategy": strategy,
                "state": _decision_state(payload),
                "action": payload.get("action"),
                "hypothesisSide": _decision_hypothesis_side(payload),
                "confidence": payload.get("confidence"),
                "watchedLevel": payload.get("watched_level"),
                "reasons": list(payload.get("reasons") or []),
            }
    return [latest[key] for key in sorted(latest)]


def _classify_move_visibility(
    rows: list[dict],
    *,
    symbol: str,
    side: str,
    start_ts: float,
    threshold_ts: float,
    decision_lookback_seconds: float,
) -> dict:
    window_start = start_ts - max(0.0, decision_lookback_seconds)
    latest = _latest_decisions_before(
        rows,
        symbol=symbol,
        end_ts=threshold_ts,
    )

    matching_strategies: set[str] = set()
    matching_wait_strategies: set[str] = set()
    tradeable_strategies: set[str] = set()
    execution_events: list[str] = []

    for item in latest:
        strategy = str(item.get("strategy") or "")
        state = str(item.get("state") or "")
        hypothesis_side = item.get("hypothesisSide")
        action = str(item.get("action") or "")
        if hypothesis_side != side:
            continue
        if strategy:
            matching_strategies.add(strategy)
        if action == side:
            tradeable_strategies.add(strategy)
        elif state in TRACKED_STATES.get(strategy, set()):
            matching_wait_strategies.add(strategy)

    for row in rows:
        if row.get("symbol") != symbol:
            continue
        ts = _row_ts(row)
        if ts < window_start or ts > threshold_ts:
            continue
        event = str(row.get("event") or "")
        payload = row.get("payload") or {}
        if event == "trade_opened":
            plan = payload.get("plan") or {}
            event_side = str(plan.get("side") or payload.get("side") or "")
            if event_side == side:
                execution_events.append("trade_opened")
        elif event == "entry_pending":
            plan = payload.get("plan") or {}
            event_side = str(plan.get("side") or payload.get("side") or "")
            if event_side == side:
                execution_events.append("entry_pending")
        elif event in {"risk_reject", "setup_blocked"}:
            decision = payload.get("decision") or {}
            if isinstance(decision, dict):
                event_side = str(decision.get("action") or "")
                if event_side == side:
                    execution_events.append(event)

    if "trade_opened" in execution_events:
        status = "traded"
    elif tradeable_strategies or execution_events:
        status = "detected_not_executed"
    elif matching_wait_strategies:
        status = "observed_not_tradeable"
    else:
        status = "undetected"

    return {
        "visibility": status,
        "matchingStrategies": sorted(matching_strategies),
        "matchingWaitStrategies": sorted(matching_wait_strategies),
        "tradeableStrategies": sorted(tradeable_strategies),
        "executionEvents": sorted(set(execution_events)),
        "strategySnapshot": latest,
    }


def _market_move_census(
    rows: list[dict],
    observations: dict[str, list[dict]],
    *,
    minimum_move_pct: float,
    maximum_threshold_seconds: float,
    decision_lookback_seconds: float,
) -> list[dict]:
    result: list[dict] = []
    move_index = 0
    for symbol in sorted(observations):
        moves = _significant_market_moves(
            observations[symbol],
            minimum_move_pct=minimum_move_pct,
            maximum_threshold_seconds=maximum_threshold_seconds,
        )
        for move in moves:
            move_index += 1
            visibility = _classify_move_visibility(
                rows,
                symbol=symbol,
                side=str(move["side"]),
                start_ts=float(move["startTs"]),
                threshold_ts=float(move["thresholdTs"]),
                decision_lookback_seconds=decision_lookback_seconds,
            )
            result.append({
                "reviewId": f"market-move-{move_index}",
                "symbol": symbol,
                **move,
                **visibility,
            })
    result.sort(key=lambda item: float(item["startTs"]), reverse=True)
    return result


def analyze_session_rows(
    rows: list[dict],
    *,
    horizon_seconds: float = 120.0,
    market_move_pct: float = DEFAULT_MARKET_MOVE_PCT,
    market_move_horizon_seconds: float = DEFAULT_MARKET_MOVE_HORIZON_SECONDS,
    market_move_decision_lookback_seconds: float = DEFAULT_MARKET_MOVE_DECISION_LOOKBACK_SECONDS,
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

    market_moves = _market_move_census(
        rows,
        observations,
        minimum_move_pct=max(0.0001, market_move_pct),
        maximum_threshold_seconds=max(10.0, market_move_horizon_seconds),
        decision_lookback_seconds=max(
            0.0,
            market_move_decision_lookback_seconds,
        ),
    )
    hindsight = analyze_hindsight_opportunities(rows)

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

    market_move_status = Counter(
        str(row.get("visibility") or "unknown")
        for row in market_moves
    )

    return {
        "horizonSeconds": horizon_seconds,
        "marketMovePolicy": {
            "minimumMovePct": market_move_pct,
            "maximumThresholdSeconds": market_move_horizon_seconds,
            "decisionLookbackSeconds": market_move_decision_lookback_seconds,
            "priceSource": (
                "research/market frame lastPrice, candle close fallback; "
                "forming-candle high/low is not used for move discovery"
            ),
        },
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
            "significantMarketMoves": len(market_moves),
            "undetectedMarketMoves": market_move_status["undetected"],
            "observedNotTradeableMarketMoves": market_move_status[
                "observed_not_tradeable"
            ],
            "detectedNotExecutedMarketMoves": market_move_status[
                "detected_not_executed"
            ],
            "tradedMarketMoves": market_move_status["traded"],
            "hindsightOpportunities": hindsight["summary"]["opportunities"],
            "hindsightMissed": hindsight["summary"]["botMissed"],
            "hindsightMapped": hindsight["summary"]["mappedToExistingStrategy"],
            "hindsightUnmapped": hindsight["summary"]["unmappedToExistingStrategy"],
            "hindsightLateEntry": hindsight["summary"]["botLateEntry"],
            "hindsightEarlyExit": hindsight["summary"]["botEarlyExit"],
        },
        "byReason": dict(by_reason),
        "byStrategy": by_strategy,
        "candidates": candidates,
        "earlyExits": early_exits,
        "marketMoves": market_moves,
        "hindsight": hindsight,
    }
