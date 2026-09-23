from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


DEFAULT_TAKER_FEE_RATE = 0.00055
DEFAULT_SLIPPAGE_BPS = 1.0
DEFAULT_MIN_NET_MOVE_PCT = 0.0010
DEFAULT_REVERSAL_PCT = 0.0015
DEFAULT_ENTRY_WINDOW_FRACTION = 0.25
DEFAULT_EXIT_WINDOW_FRACTION = 0.80

STRATEGY_STATE_ORDER = {
    "trend_structure": {
        "search": 0,
        "pullback": 1,
        "test": 2,
        "reclaim": 3,
        "continuation": 4,
    },
    "weak_level_rejection": {
        "search": 0,
        "found": 1,
        "approach": 2,
        "test": 3,
        "reject": 4,
        "reaction": 5,
    },
    "orderbook_density": {
        "search": 0,
        "found": 1,
        "persisting": 2,
        "approach": 3,
        "test": 4,
        "defended": 5,
        "reaction": 6,
        "exhausted": 0,
    },
    "level_breakout": {
        "search": 0,
        "found": 1,
        "approach": 2,
        "pressure": 3,
        "break": 4,
        "impulse": 5,
    },
}


@dataclass(slots=True)
class PricePoint:
    ts: float
    price: float


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _frame_price(row: dict) -> float | None:
    payload = row.get("payload") or {}
    direct = payload.get("lastPrice")
    if isinstance(direct, (int, float)) and direct > 0:
        return float(direct)

    candle = payload.get("candle")
    if isinstance(candle, dict):
        close = candle.get("close")
        if isinstance(close, (int, float)) and close > 0:
            return float(close)

    market = payload.get("market")
    if isinstance(market, dict):
        direct = market.get("lastPrice")
        if isinstance(direct, (int, float)) and direct > 0:
            return float(direct)
        candles = market.get("candles") or []
        if candles:
            close = candles[-1].get("close")
            if isinstance(close, (int, float)) and close > 0:
                return float(close)
    return None


def _price_segments(rows: list[dict]) -> dict[str, list[list[PricePoint]]]:
    by_symbol: dict[str, list[list[PricePoint]]] = defaultdict(list)
    current: dict[str, list[PricePoint]] = {}
    saw_lifecycle: set[str] = set()

    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        event = str(row.get("event") or "")

        if event == "symbol_activated":
            saw_lifecycle.add(symbol)
            segment: list[PricePoint] = []
            current[symbol] = segment
            by_symbol[symbol].append(segment)
            continue

        if event == "symbol_deactivated":
            saw_lifecycle.add(symbol)
            current.pop(symbol, None)
            continue

        if event not in {"research_frame", "market_frame"}:
            continue

        price = _frame_price(row)
        if price is None:
            continue

        segment = current.get(symbol)
        if segment is None:
            # Old/synthetic sessions may not have activation events.
            if symbol in saw_lifecycle:
                continue
            if not by_symbol[symbol]:
                segment = []
                by_symbol[symbol].append(segment)
            else:
                segment = by_symbol[symbol][-1]
            current[symbol] = segment

        ts = _row_ts(row)
        if segment and ts == segment[-1].ts:
            segment[-1] = PricePoint(ts=ts, price=price)
        elif not segment or ts > segment[-1].ts:
            segment.append(PricePoint(ts=ts, price=price))

    return {
        symbol: [segment for segment in segments if len(segment) >= 2]
        for symbol, segments in by_symbol.items()
    }


def _directional_move(start: float, end: float, side: str) -> float:
    if start <= 0:
        return 0.0
    signed = (end - start) / start
    return signed if side == "long" else -signed


def _first_fraction_point(
    points: list[PricePoint],
    *,
    start_index: int,
    end_index: int,
    side: str,
    total_move_pct: float,
    fraction: float,
) -> PricePoint:
    target_move = max(0.0, total_move_pct * fraction)
    start = points[start_index]
    for point in points[start_index:end_index + 1]:
        if _directional_move(start.price, point.price, side) >= target_move:
            return point
    return points[end_index]


def _swing(
    points: list[PricePoint],
    *,
    side: str,
    start_index: int,
    end_index: int,
    gross_required_pct: float,
    cost_pct: float,
    entry_window_fraction: float,
    exit_window_fraction: float,
) -> dict | None:
    if end_index <= start_index:
        return None
    start = points[start_index]
    end = points[end_index]
    gross_move_pct = _directional_move(start.price, end.price, side)
    if gross_move_pct < gross_required_pct:
        return None

    confirmation = _first_fraction_point(
        points,
        start_index=start_index,
        end_index=end_index,
        side=side,
        total_move_pct=gross_move_pct,
        fraction=min(1.0, gross_required_pct / gross_move_pct),
    )
    entry_window_end = _first_fraction_point(
        points,
        start_index=start_index,
        end_index=end_index,
        side=side,
        total_move_pct=gross_move_pct,
        fraction=entry_window_fraction,
    )
    exit_window_start = _first_fraction_point(
        points,
        start_index=start_index,
        end_index=end_index,
        side=side,
        total_move_pct=gross_move_pct,
        fraction=exit_window_fraction,
    )

    return {
        "side": side,
        "oracleEntryTs": start.ts,
        "oracleEntryPrice": start.price,
        "oracleExitTs": end.ts,
        "oracleExitPrice": end.price,
        "confirmationTs": confirmation.ts,
        "confirmationPrice": confirmation.price,
        "entryWindowEndTs": entry_window_end.ts,
        "entryWindowEndPrice": entry_window_end.price,
        "exitWindowStartTs": exit_window_start.ts,
        "exitWindowStartPrice": exit_window_start.price,
        "grossMovePct": gross_move_pct,
        "estimatedRoundTripCostPct": cost_pct,
        "estimatedNetMovePct": max(0.0, gross_move_pct - cost_pct),
        "durationSeconds": max(0.0, end.ts - start.ts),
    }


def _segment_swings(
    points: list[PricePoint],
    *,
    gross_required_pct: float,
    reversal_pct: float,
    cost_pct: float,
    entry_window_fraction: float,
    exit_window_fraction: float,
) -> list[dict]:
    if len(points) < 2:
        return []

    swings: list[dict] = []
    direction: str | None = None

    high_index = low_index = 0
    high_price = low_price = points[0].price

    pivot_index = 0
    extreme_index = 0
    extreme_price = points[0].price

    for index in range(1, len(points)):
        price = points[index].price

        if direction is None:
            if price >= high_price:
                high_price = price
                high_index = index
            if price <= low_price:
                low_price = price
                low_index = index

            up_move = _directional_move(low_price, price, "long")
            down_move = _directional_move(high_price, price, "short")
            if up_move >= gross_required_pct and low_index < index:
                direction = "long"
                pivot_index = low_index
                extreme_index = index
                extreme_price = price
            elif down_move >= gross_required_pct and high_index < index:
                direction = "short"
                pivot_index = high_index
                extreme_index = index
                extreme_price = price
            continue

        if direction == "long":
            if price >= extreme_price:
                extreme_price = price
                extreme_index = index
                continue
            if _directional_move(extreme_price, price, "short") < reversal_pct:
                continue

            swing = _swing(
                points,
                side="long",
                start_index=pivot_index,
                end_index=extreme_index,
                gross_required_pct=gross_required_pct,
                cost_pct=cost_pct,
                entry_window_fraction=entry_window_fraction,
                exit_window_fraction=exit_window_fraction,
            )
            if swing is not None:
                swings.append(swing)

            direction = "short"
            pivot_index = extreme_index
            extreme_index = index
            extreme_price = price
            continue

        if price <= extreme_price:
            extreme_price = price
            extreme_index = index
            continue
        if _directional_move(extreme_price, price, "long") < reversal_pct:
            continue

        swing = _swing(
            points,
            side="short",
            start_index=pivot_index,
            end_index=extreme_index,
            gross_required_pct=gross_required_pct,
            cost_pct=cost_pct,
            entry_window_fraction=entry_window_fraction,
            exit_window_fraction=exit_window_fraction,
        )
        if swing is not None:
            swings.append(swing)

        direction = "long"
        pivot_index = extreme_index
        extreme_index = index
        extreme_price = price

    if direction is not None:
        swing = _swing(
            points,
            side=direction,
            start_index=pivot_index,
            end_index=extreme_index,
            gross_required_pct=gross_required_pct,
            cost_pct=cost_pct,
            entry_window_fraction=entry_window_fraction,
            exit_window_fraction=exit_window_fraction,
        )
        if swing is not None:
            swings.append(swing)

    return swings


def _decision_state(payload: dict) -> str:
    details = payload.get("details") or {}
    trace = payload.get("trace") or {}
    return str(details.get("state") or trace.get("state") or "")


def _decision_side(payload: dict) -> str | None:
    action = str(payload.get("action") or "")
    if action in {"long", "short"}:
        return action

    strategy = str(payload.get("strategy") or "")
    details = payload.get("details") or {}
    trace = payload.get("trace") or {}
    obj = trace.get("object") or {}
    if not isinstance(obj, dict):
        obj = {}

    if strategy == "trend_structure":
        trend = str(trace.get("trend") or details.get("trend") or "")
        if trend == "up":
            return "long"
        if trend == "down":
            return "short"

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
        if wall_side == "bid":
            return "long"
        if wall_side == "ask":
            return "short"
    return None


def _decision_index(rows: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("event") != "decision":
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        payload = row.get("payload") or {}
        strategy = str(payload.get("strategy") or "")
        if not strategy:
            continue
        result[symbol].append({
            "ts": _row_ts(row),
            "strategy": strategy,
            "state": _decision_state(payload),
            "action": str(payload.get("action") or ""),
            "hypothesisSide": _decision_side(payload),
            "confidence": payload.get("confidence"),
            "watchedLevel": payload.get("watched_level"),
            "reasons": list(payload.get("reasons") or []),
        })
    return result


def _state_rank(strategy: str, state: str) -> int:
    return STRATEGY_STATE_ORDER.get(strategy, {}).get(state, 0)


def _strategy_fit(
    decision_rows: list[dict],
    *,
    side: str,
    start_ts: float,
    entry_window_end_ts: float,
) -> dict:
    by_strategy: dict[str, dict] = {}
    relevant = [
        row
        for row in decision_rows
        if start_ts - 30.0 <= row["ts"] <= entry_window_end_ts
    ]

    for row in relevant:
        strategy = row["strategy"]
        bucket = by_strategy.setdefault(
            strategy,
            {
                "strategy": strategy,
                "statesSeen": [],
                "alignedStatesSeen": [],
                "opposedStatesSeen": [],
                "tradeableAligned": False,
                "tradeableOpposed": False,
                "firstAlignedTs": None,
                "firstTradeableTs": None,
                "maxConfidence": None,
                "lastReason": None,
                "bestState": None,
                "bestStateRank": 0,
            },
        )
        state = str(row.get("state") or "")
        if state and state not in bucket["statesSeen"]:
            bucket["statesSeen"].append(state)

        hypothesis_side = row.get("hypothesisSide")
        aligned = hypothesis_side == side
        opposed = hypothesis_side in {"long", "short"} and hypothesis_side != side

        if aligned:
            if state and state not in bucket["alignedStatesSeen"]:
                bucket["alignedStatesSeen"].append(state)
            if bucket["firstAlignedTs"] is None:
                bucket["firstAlignedTs"] = row["ts"]
            rank = _state_rank(strategy, state)
            if rank >= bucket["bestStateRank"]:
                bucket["bestStateRank"] = rank
                bucket["bestState"] = state
            if row.get("action") == side:
                bucket["tradeableAligned"] = True
                if bucket["firstTradeableTs"] is None:
                    bucket["firstTradeableTs"] = row["ts"]
        elif opposed:
            if state and state not in bucket["opposedStatesSeen"]:
                bucket["opposedStatesSeen"].append(state)
            if row.get("action") in {"long", "short"}:
                bucket["tradeableOpposed"] = True

        confidence = row.get("confidence")
        if isinstance(confidence, (int, float)):
            current = bucket["maxConfidence"]
            bucket["maxConfidence"] = (
                float(confidence)
                if current is None
                else max(float(current), float(confidence))
            )
        reasons = row.get("reasons") or []
        if reasons:
            bucket["lastReason"] = reasons[-1]

    rows_out = []
    for strategy in sorted(STRATEGY_STATE_ORDER):
        bucket = by_strategy.get(strategy)
        if bucket is None:
            bucket = {
                "strategy": strategy,
                "statesSeen": [],
                "alignedStatesSeen": [],
                "opposedStatesSeen": [],
                "tradeableAligned": False,
                "tradeableOpposed": False,
                "firstAlignedTs": None,
                "firstTradeableTs": None,
                "maxConfidence": None,
                "lastReason": None,
                "bestState": None,
                "bestStateRank": 0,
            }

        if bucket["tradeableAligned"]:
            fit = "tradeable_aligned"
        elif bucket["alignedStatesSeen"]:
            fit = "observed_aligned"
        elif bucket["opposedStatesSeen"]:
            fit = "opposed"
        else:
            fit = "unaware"
        bucket["fit"] = fit
        rows_out.append(bucket)

    aligned = [
        row
        for row in rows_out
        if row["fit"] in {"tradeable_aligned", "observed_aligned"}
    ]
    aligned.sort(
        key=lambda row: (
            row["tradeableAligned"],
            row["bestStateRank"],
            row["maxConfidence"] or 0.0,
        ),
        reverse=True,
    )
    return {
        "closestPlaybook": aligned[0]["strategy"] if aligned else None,
        "mappedToExistingStrategy": bool(aligned),
        "strategies": rows_out,
    }


def _trade_pairs(rows: list[dict]) -> dict[str, list[dict]]:
    pending: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    result: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        event = str(row.get("event") or "")
        payload = row.get("payload") or {}
        if event == "trade_opened":
            plan = payload.get("plan") or {}
            position = payload.get("position") or {}
            setup_id = (
                plan.get("setup_id")
                or plan.get("setupId")
                or position.get("setup_id")
                or position.get("setupId")
            )
            pending[(symbol, str(setup_id) if setup_id is not None else None)].append({
                "openTs": _row_ts(row),
                "side": str(position.get("side") or plan.get("side") or ""),
                "entry": position.get("entry") or plan.get("market_entry"),
                "strategy": plan.get("strategy") or payload.get("strategy"),
                "setupId": setup_id,
            })
            continue

        if event != "trade_closed":
            continue

        setup_id = payload.get("setupId") or payload.get("setup_id")
        key = (symbol, str(setup_id) if setup_id is not None else None)
        queue = pending.get(key)
        if not queue:
            fallback = next(
                (
                    candidate
                    for candidate, values in pending.items()
                    if candidate[0] == symbol and values
                ),
                None,
            )
            queue = pending.get(fallback) if fallback is not None else None
        if not queue:
            continue

        trade = queue.pop(0)
        trade.update({
            "closeTs": _row_ts(row),
            "exit": payload.get("exit"),
            "netPnl": payload.get("netPnl"),
            "reason": payload.get("reason"),
        })
        result[symbol].append(trade)

    for (symbol, _setup_id), queue in pending.items():
        for trade in queue:
            result[symbol].append(trade)
    return result


def _bot_comparison(
    trades: list[dict],
    *,
    side: str,
    start_ts: float,
    end_ts: float,
    entry_price: float,
    exit_price: float,
) -> dict:
    total_move = abs(exit_price - entry_price)
    overlapping = [
        trade
        for trade in trades
        if trade["openTs"] <= end_ts
        and (trade.get("closeTs") is None or trade["closeTs"] >= start_ts)
    ]
    same = [
        trade
        for trade in overlapping
        if trade.get("side") == side
        and start_ts <= trade["openTs"] <= end_ts
    ]
    opposite = [
        trade
        for trade in overlapping
        if trade.get("side") in {"long", "short"}
        and trade.get("side") != side
    ]

    if not same:
        return {
            "classification": "wrong_direction" if opposite else "missed",
            "trade": opposite[0] if opposite else None,
            "entryDelaySeconds": None,
            "entryMoveAlreadySpentPct": None,
            "remainingMoveCapturePct": None,
            "exitCapturePct": None,
        }

    trade = min(same, key=lambda item: item["openTs"])
    trade_entry = trade.get("entry")
    spent = None
    remaining = None
    if isinstance(trade_entry, (int, float)) and total_move > 0:
        if side == "long":
            spent_abs = max(0.0, float(trade_entry) - entry_price)
            remaining_abs = max(0.0, exit_price - float(trade_entry))
        else:
            spent_abs = max(0.0, entry_price - float(trade_entry))
            remaining_abs = max(0.0, float(trade_entry) - exit_price)
        spent = min(1.0, spent_abs / total_move)
        remaining = min(1.0, remaining_abs / total_move)

    exit_capture = None
    trade_exit = trade.get("exit")
    if isinstance(trade_exit, (int, float)) and total_move > 0:
        if side == "long":
            captured = float(trade_exit) - entry_price
        else:
            captured = entry_price - float(trade_exit)
        exit_capture = max(-1.0, min(1.5, captured / total_move))

    classification = "traded"
    if spent is not None and spent > DEFAULT_ENTRY_WINDOW_FRACTION:
        classification = "late_entry"
    if (
        trade.get("closeTs") is not None
        and trade["closeTs"] < end_ts
        and exit_capture is not None
        and exit_capture < DEFAULT_EXIT_WINDOW_FRACTION
    ):
        classification = "early_exit" if classification == "traded" else f"{classification}_early_exit"

    return {
        "classification": classification,
        "trade": trade,
        "entryDelaySeconds": max(0.0, trade["openTs"] - start_ts),
        "entryMoveAlreadySpentPct": spent,
        "remainingMoveCapturePct": remaining,
        "exitCapturePct": exit_capture,
    }


def analyze_hindsight_opportunities(
    rows: list[dict],
    *,
    taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    minimum_net_move_pct: float = DEFAULT_MIN_NET_MOVE_PCT,
    reversal_pct: float = DEFAULT_REVERSAL_PCT,
    entry_window_fraction: float = DEFAULT_ENTRY_WINDOW_FRACTION,
    exit_window_fraction: float = DEFAULT_EXIT_WINDOW_FRACTION,
) -> dict[str, Any]:
    cost_pct = max(0.0, taker_fee_rate) * 2.0 + max(0.0, slippage_bps) * 2.0 / 10_000
    gross_required_pct = cost_pct + max(0.0, minimum_net_move_pct)
    reversal = max(0.0001, reversal_pct)
    segments = _price_segments(rows)
    decisions = _decision_index(rows)
    trades = _trade_pairs(rows)

    opportunities: list[dict] = []
    sequence = 0
    for symbol in sorted(segments):
        for segment_index, points in enumerate(segments[symbol], start=1):
            swings = _segment_swings(
                points,
                gross_required_pct=gross_required_pct,
                reversal_pct=reversal,
                cost_pct=cost_pct,
                entry_window_fraction=entry_window_fraction,
                exit_window_fraction=exit_window_fraction,
            )
            for swing in swings:
                sequence += 1
                fit = _strategy_fit(
                    decisions.get(symbol, []),
                    side=swing["side"],
                    start_ts=swing["oracleEntryTs"],
                    entry_window_end_ts=swing["entryWindowEndTs"],
                )
                bot = _bot_comparison(
                    trades.get(symbol, []),
                    side=swing["side"],
                    start_ts=swing["oracleEntryTs"],
                    end_ts=swing["oracleExitTs"],
                    entry_price=swing["oracleEntryPrice"],
                    exit_price=swing["oracleExitPrice"],
                )
                opportunities.append({
                    "opportunityId": f"hindsight-{sequence}",
                    "symbol": symbol,
                    "segment": segment_index,
                    **swing,
                    "strategyFit": fit,
                    "botComparison": bot,
                })

    opportunities.sort(key=lambda item: item["oracleEntryTs"])

    bot_counts: dict[str, int] = defaultdict(int)
    mapped = 0
    unmapped = 0
    for item in opportunities:
        bot_counts[str(item["botComparison"]["classification"])] += 1
        if item["strategyFit"]["mappedToExistingStrategy"]:
            mapped += 1
        else:
            unmapped += 1

    return {
        "schemaVersion": 1,
        "policy": {
            "priceSource": (
                "recorded research/market-frame lastPrice; candle close fallback. "
                "Strategy decisions are not used to discover opportunities."
            ),
            "estimatedRoundTripCostPct": cost_pct,
            "minimumNetMovePct": minimum_net_move_pct,
            "minimumGrossMovePct": gross_required_pct,
            "reversalPct": reversal,
            "entryWindowFraction": entry_window_fraction,
            "exitWindowFraction": exit_window_fraction,
            "hindsightWarning": (
                "Oracle entry/exit use future information and are labels for research, "
                "not executable signals. Strategy features must be evaluated only from "
                "information available before or inside the entry window."
            ),
        },
        "summary": {
            "opportunities": len(opportunities),
            "mappedToExistingStrategy": mapped,
            "unmappedToExistingStrategy": unmapped,
            "botMissed": bot_counts["missed"],
            "botWrongDirection": bot_counts["wrong_direction"],
            "botTraded": bot_counts["traded"],
            "botLateEntry": sum(
                count
                for key, count in bot_counts.items()
                if key.startswith("late_entry")
            ),
            "botEarlyExit": sum(
                count
                for key, count in bot_counts.items()
                if "early_exit" in key
            ),
        },
        "botClassificationCounts": dict(bot_counts),
        "opportunities": opportunities,
    }
