from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from typing import Any


DEFAULT_HORIZONS_SECONDS = (30.0, 60.0, 120.0, 300.0)
DEFAULT_MOVE_BANDS = (0.001, 0.002, 0.003, 0.005, 0.010)
DEFAULT_MAX_SHARED_LEVEL_DISTANCE_PCT = 0.006

TRACKED_STATES = {
    "trend_structure": {"pullback", "test", "reclaim", "continuation"},
    "weak_level_rejection": {"approach", "test", "reject", "reaction"},
    "orderbook_density": {"approach", "test", "defended", "reaction", "exhausted"},
    "level_breakout": {"approach", "pressure", "break", "impulse"},
}

HYPOTHESIS_KIND = {
    "trend_structure": "trend_continuation",
    "weak_level_rejection": "level_rejection",
    "orderbook_density": "liquidity_evidence",
    "level_breakout": "level_breakout",
}

PLAYBOOK_STRATEGIES = {
    "trend_structure",
    "weak_level_rejection",
    "level_breakout",
}


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _sampled_price(row: dict) -> float | None:
    payload = row.get("payload") or {}
    last_price = payload.get("lastPrice")
    if isinstance(last_price, (int, float)) and last_price > 0:
        return float(last_price)
    candle = payload.get("candle")
    if isinstance(candle, dict):
        close = candle.get("close")
        if isinstance(close, (int, float)) and close > 0:
            return float(close)
    return None


def _state(decision: dict) -> str:
    details = decision.get("details") or {}
    trace = decision.get("trace") or {}
    return str(details.get("state") or trace.get("state") or "")


def _trace_object(decision: dict) -> dict:
    trace = decision.get("trace") or {}
    obj = trace.get("object")
    return dict(obj) if isinstance(obj, dict) else {}


def _reference_level(decision: dict) -> float | None:
    obj = _trace_object(decision)
    if obj.get("low") is not None and obj.get("high") is not None:
        return (float(obj["low"]) + float(obj["high"])) / 2.0
    if obj.get("price") is not None:
        return float(obj["price"])

    details = decision.get("details") or {}
    zone = details.get("zone")
    if isinstance(zone, dict) and zone.get("low") is not None and zone.get("high") is not None:
        return (float(zone["low"]) + float(zone["high"])) / 2.0
    for key in ("wallPrice", "projectedTrendlinePrice"):
        value = details.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    watched = decision.get("watched_level")
    if isinstance(watched, (int, float)) and watched > 0:
        return float(watched)
    return None


def _object_id(decision: dict) -> str:
    details = decision.get("details") or {}
    for key in ("zoneGeneration", "levelGeneration", "trendlineAnchor"):
        value = details.get(key)
        if value is not None:
            if isinstance(value, (list, tuple)):
                return f"{key}:" + "|".join(str(item) for item in value)
            return f"{key}:{value}"

    wall_side = details.get("wallSide")
    wall_price = details.get("wallPrice")
    if wall_side is not None and wall_price is not None:
        return f"wall:{wall_side}:{float(wall_price):.10g}"

    obj = _trace_object(decision)
    kind = str(obj.get("type") or "object")
    label = str(obj.get("label") or "")
    if obj.get("low") is not None and obj.get("high") is not None:
        return (
            f"{kind}:{label}:"
            f"{float(obj['low']):.10g}:{float(obj['high']):.10g}"
        )
    if obj.get("price") is not None:
        return f"{kind}:{label}:{float(obj['price']):.10g}"
    level = _reference_level(decision)
    return f"{kind}:{label}:{float(level or 0.0):.10g}"


def _hypothesis_side(decision: dict) -> str | None:
    action = str(decision.get("action") or "")
    if action in {"long", "short"}:
        return action

    strategy = str(decision.get("strategy") or "")
    details = decision.get("details") or {}
    trace = decision.get("trace") or {}
    obj = _trace_object(decision)

    if strategy == "trend_structure":
        trend = str(trace.get("trend") or details.get("trend") or "")
        if trend == "up":
            return "long"
        if trend == "down":
            return "short"
        return None

    zone = details.get("zone") or {}
    label = " ".join(
        str(value or "")
        for value in (
            obj.get("label"),
            obj.get("kind"),
            zone.get("kind") if isinstance(zone, dict) else None,
        )
    ).lower()

    if strategy == "level_breakout":
        if "resistance" in label:
            return "long"
        if "support" in label:
            return "short"
        return None

    if strategy == "weak_level_rejection":
        if "resistance" in label:
            return "short"
        if "support" in label:
            return "long"
        return None

    if strategy == "orderbook_density":
        liquidity = details.get("liquidityEvidence") or {}
        if isinstance(liquidity, dict):
            directional_bias = str(
                liquidity.get("directionalBias") or ""
            )
            if directional_bias == "up":
                return "long"
            if directional_bias == "down":
                return "short"
            if directional_bias == "flat":
                return None
        wall_side = str(details.get("wallSide") or obj.get("side") or "")
        if wall_side == "ask":
            return "short"
        if wall_side == "bid":
            return "long"
    return None


def _feature_snapshot(decision: dict, anchor_price: float) -> dict[str, Any]:
    details = decision.get("details") or {}
    trace = decision.get("trace") or {}
    evidence = trace.get("evidence") or {}

    def mapping(name: str) -> dict:
        value = details.get(name)
        if not isinstance(value, dict):
            value = evidence.get(name)
        return value if isinstance(value, dict) else {}

    flow = mapping("flow")
    multi_flow = mapping("multiHorizonFlow")
    flow_alignment = mapping("flowAlignment")
    liquidity_evidence = mapping("liquidityEvidence")
    liquidity_alignment = mapping("liquidityAlignment")
    decision_context = mapping("decisionContext")
    entry_freshness = mapping("entryFreshness")
    horizons = (
        multi_flow.get("horizons")
        if isinstance(multi_flow.get("horizons"), dict)
        else {}
    )
    flow_5 = horizons.get("5s") if isinstance(horizons.get("5s"), dict) else {}
    flow_15 = horizons.get("15s") if isinstance(horizons.get("15s"), dict) else {}
    flow_60 = horizons.get("60s") if isinstance(horizons.get("60s"), dict) else {}
    level_flow = mapping("levelFlow")
    recent_level_flow = mapping("recentLevelFlow")
    zone = details.get("zone")
    zone = zone if isinstance(zone, dict) else {}
    lifecycle = details.get("levelLifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    pullback = details.get("pullbackCharacter")
    pullback = pullback if isinstance(pullback, dict) else {}
    obstacle = details.get("nearestObstacle")
    obstacle = obstacle if isinstance(obstacle, dict) else {}

    obstacle_price = obstacle.get("price")
    obstacle_distance = None
    if isinstance(obstacle_price, (int, float)) and anchor_price > 0:
        obstacle_distance = abs(float(obstacle_price) - anchor_price) / anchor_price

    fields = {
        "confidence": decision.get("confidence"),
        "setupQuality": details.get("setupQuality"),
        "contextHtfBias": decision_context.get("htfBias"),
        "contextLocalRegime": decision_context.get("localRegime"),
        "contextLocalDirection": decision_context.get("localDirection"),
        "contextExecutionReady": decision_context.get("executionReady"),
        "contextSpreadPct": decision_context.get("spreadPct"),
        "contextTop5DepthUsd": decision_context.get("top5DepthUsd"),
        "contextNearestSupportDistancePct": decision_context.get(
            "nearestSupportDistancePct"
        ),
        "contextNearestResistanceDistancePct": decision_context.get(
            "nearestResistanceDistancePct"
        ),
        "entryFreshnessClass": entry_freshness.get("classification"),
        "entryMoveSpentRatio": entry_freshness.get("moveSpentRatio"),
        "entryConfirmationAgeSeconds": entry_freshness.get(
            "confirmationAgeSeconds"
        ),
        "pressureScore": details.get("pressureScore"),
        "flowParticipationConfirmed": flow.get("participationConfirmed"),
        "flowImbalance5s": flow.get("imbalance5s"),
        "flowImbalance15s": flow.get("imbalance15s"),
        "flowImbalance60s": flow.get("imbalance60s"),
        "flowNotionalPerSecond5s": flow.get("notionalPerSecond5s"),
        "flowAcceleration": flow.get("acceleration"),
        "flowTradeRateRatio": flow.get("tradeRateRatio"),
        "flowTradeSizeRatio": flow.get("tradeSizeRatio"),
        "cvd5s": flow.get("cvd5s"),
        "cvd15s": flow.get("cvd15s"),
        "cvd60s": flow.get("cvd60s"),
        "flowAlignmentClass": flow_alignment.get("classification"),
        "flowAlignmentScore": flow_alignment.get("score"),
        "flowDominantDirection": multi_flow.get("dominantDirection"),
        "flowDirectionalScore": multi_flow.get("directionalScore"),
        "flowCoherence": multi_flow.get("coherence"),
        "flow5Score": flow_5.get("score"),
        "flow15Score": flow_15.get("score"),
        "flow60Score": flow_60.get("score"),
        "flow5Direction": flow_5.get("direction"),
        "flow15Direction": flow_15.get("direction"),
        "flow60Direction": flow_60.get("direction"),
        "normalizedOfi5s": flow_5.get("normalizedOfi"),
        "normalizedOfi15s": flow_15.get("normalizedOfi"),
        "normalizedOfi60s": flow_60.get("normalizedOfi"),
        "liquidityEvidenceState": liquidity_evidence.get("state"),
        "liquidityDirectionalBias": liquidity_evidence.get("directionalBias"),
        "liquidityDirectionalStrength": liquidity_evidence.get("directionalStrength"),
        "liquidityAlignmentClass": liquidity_alignment.get("classification"),
        "liquidityAlignmentScore": liquidity_alignment.get("score"),
        "liquidityWallSide": liquidity_evidence.get("wallSide"),
        "liquidityWallPrice": liquidity_evidence.get("wallPrice"),
        "liquidityWallPresent": liquidity_evidence.get("wallPresent"),
        "liquidityRemainingRatio": liquidity_evidence.get("remainingRatio"),
        "liquidityAttackRatio": liquidity_evidence.get("attackRatio"),
        "liquidityDepletionPerSecond": liquidity_evidence.get("depletionPerSecond"),
        "liquidityReplenishmentRatio": liquidity_evidence.get("replenishmentRatio"),
        "liquidityAbsorptionObserved": liquidity_evidence.get("absorptionObserved"),
        "liquidityConsumptionCauses": liquidity_evidence.get("consumptionCauses"),
        "levelFlowImbalance": level_flow.get("imbalance"),
        "levelFlowTradeCount": level_flow.get("tradeCount"),
        "levelFlowPriceResponsePct": level_flow.get("priceResponsePct"),
        "levelFlowAbsorptionEfficiency": level_flow.get("absorptionEfficiency"),
        "recentLevelFlowImbalance": recent_level_flow.get("imbalance"),
        "recentLevelFlowTradeCount": recent_level_flow.get("tradeCount"),
        "recentLevelFlowAbsorptionEfficiency": recent_level_flow.get("absorptionEfficiency"),
        "zoneTouches": zone.get("touches"),
        "zoneReactionPct": zone.get("reaction_pct"),
        "zoneVolumeRatio": zone.get("volume_ratio"),
        "zoneScore": zone.get("score"),
        "levelScore": lifecycle.get("score"),
        "distinctApproaches": lifecycle.get("distinct_approaches"),
        "dwellBars": lifecycle.get("dwell_bars"),
        "acceptanceBars": lifecycle.get("acceptance_bars"),
        "failedBreaks": lifecycle.get("failed_breaks"),
        "sweeps": lifecycle.get("sweeps"),
        "levelLifecycle": lifecycle.get("lifecycle"),
        "remainingRatio": details.get("remainingRatio"),
        "attackRatio": details.get("attackRatio"),
        "depletionPerSecond": details.get("depletionPerSecond"),
        "replenishmentRatio": details.get("replenishmentRatio"),
        "strengthMultiple": details.get("strengthMultiple"),
        "absorptionObserved": details.get("absorptionObserved"),
        "wallPresent": details.get("wallPresent"),
        "wallLifetimeSeconds": details.get("lifetimeSeconds"),
        "erosionSeconds": details.get("erosionSeconds"),
        "pullbackDirectional": details.get("pullbackDirectional"),
        "pullbackVolumeRatio": pullback.get("volumeRatio"),
        "pullbackRangeRatio": pullback.get("rangeRatio"),
        "aggressiveCountertrend": pullback.get("aggressiveCountertrend"),
        "nearestObstacleDistancePct": obstacle_distance,
    }
    return {
        key: value
        for key, value in fields.items()
        if value is not None
    }


def _series_by_symbol(rows: list[dict]) -> dict[str, tuple[list[float], list[float]]]:
    raw: dict[str, dict[float, float]] = {}
    for row in rows:
        if row.get("event") not in {"research_frame", "market_frame"}:
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        price = _sampled_price(row)
        if price is None:
            continue
        raw.setdefault(symbol, {})[_row_ts(row)] = price

    result: dict[str, tuple[list[float], list[float]]] = {}
    for symbol, values in raw.items():
        ordered = sorted(values.items())
        result[symbol] = (
            [item[0] for item in ordered],
            [item[1] for item in ordered],
        )
    return result


def _band_label(band: float) -> str:
    return f"{band * 100:.2f}%"


def _directional_forward(
    series: tuple[list[float], list[float]] | None,
    *,
    start_ts: float,
    anchor_price: float,
    side: str,
    horizons_seconds: tuple[float, ...],
    move_bands: tuple[float, ...],
) -> dict:
    if series is None or anchor_price <= 0 or side not in {"long", "short"}:
        return {}

    times, prices = series
    start_index = bisect_right(times, start_ts)
    result: dict[str, dict] = {}
    for horizon in horizons_seconds:
        end_index = bisect_right(times, start_ts + horizon)
        sampled = list(zip(times[start_index:end_index], prices[start_index:end_index]))
        horizon_key = f"{horizon:g}s"
        if not sampled:
            result[horizon_key] = {
                "sampledPoints": 0,
                "finalMovePct": None,
                "maxHypothesisMovePct": None,
                "maxOppositeMovePct": None,
                "movementBands": {
                    _band_label(band): {
                        "classification": "no_data",
                        "secondsToHypothesis": None,
                        "secondsToOpposite": None,
                    }
                    for band in move_bands
                },
            }
            continue

        hypothesis_moves: list[float] = []
        opposite_moves: list[float] = []
        for _ts, price in sampled:
            signed = (price - anchor_price) / anchor_price
            hypothesis = signed if side == "long" else -signed
            hypothesis_moves.append(hypothesis)
            opposite_moves.append(-hypothesis)

        band_rows = {}
        for band in move_bands:
            hypothesis_ts = opposite_ts = None
            for (ts, _price), hypothesis, opposite in zip(
                sampled,
                hypothesis_moves,
                opposite_moves,
            ):
                if hypothesis_ts is None and hypothesis >= band:
                    hypothesis_ts = ts
                if opposite_ts is None and opposite >= band:
                    opposite_ts = ts
                if hypothesis_ts is not None and opposite_ts is not None:
                    break

            if hypothesis_ts is None and opposite_ts is None:
                classification = "unresolved"
            elif hypothesis_ts is not None and opposite_ts is None:
                classification = "hypothesis_first"
            elif opposite_ts is not None and hypothesis_ts is None:
                classification = "opposite_first"
            elif hypothesis_ts < opposite_ts:
                classification = "hypothesis_first"
            elif opposite_ts < hypothesis_ts:
                classification = "opposite_first"
            else:
                classification = "ambiguous"

            band_rows[_band_label(band)] = {
                "classification": classification,
                "secondsToHypothesis": (
                    hypothesis_ts - start_ts
                    if hypothesis_ts is not None
                    else None
                ),
                "secondsToOpposite": (
                    opposite_ts - start_ts
                    if opposite_ts is not None
                    else None
                ),
            }

        final_signed = (sampled[-1][1] - anchor_price) / anchor_price
        result[horizon_key] = {
            "sampledPoints": len(sampled),
            "finalMovePct": final_signed,
            "maxHypothesisMovePct": max(hypothesis_moves),
            "maxOppositeMovePct": max(opposite_moves),
            "movementBands": band_rows,
        }
    return result


def _neutral_forward(
    series: tuple[list[float], list[float]] | None,
    *,
    start_ts: float,
    anchor_price: float,
    horizons_seconds: tuple[float, ...],
    move_bands: tuple[float, ...],
) -> dict:
    if series is None or anchor_price <= 0:
        return {}
    times, prices = series
    start_index = bisect_right(times, start_ts)
    result: dict[str, dict] = {}

    for horizon in horizons_seconds:
        end_index = bisect_right(times, start_ts + horizon)
        sampled = list(zip(times[start_index:end_index], prices[start_index:end_index]))
        horizon_key = f"{horizon:g}s"
        if not sampled:
            result[horizon_key] = {
                "sampledPoints": 0,
                "movementBands": {
                    _band_label(band): {
                        "classification": "no_data",
                        "secondsToUp": None,
                        "secondsToDown": None,
                    }
                    for band in move_bands
                },
            }
            continue

        band_rows = {}
        for band in move_bands:
            up_ts = down_ts = None
            for ts, price in sampled:
                signed = (price - anchor_price) / anchor_price
                if up_ts is None and signed >= band:
                    up_ts = ts
                if down_ts is None and signed <= -band:
                    down_ts = ts
                if up_ts is not None and down_ts is not None:
                    break

            if up_ts is None and down_ts is None:
                classification = "unresolved"
            elif up_ts is not None and down_ts is None:
                classification = "up_first"
            elif down_ts is not None and up_ts is None:
                classification = "down_first"
            elif up_ts < down_ts:
                classification = "up_first"
            elif down_ts < up_ts:
                classification = "down_first"
            else:
                classification = "ambiguous"

            band_rows[_band_label(band)] = {
                "classification": classification,
                "secondsToUp": up_ts - start_ts if up_ts is not None else None,
                "secondsToDown": (
                    down_ts - start_ts
                    if down_ts is not None
                    else None
                ),
            }
        result[horizon_key] = {
            "sampledPoints": len(sampled),
            "movementBands": band_rows,
        }
    return result


def _checkpoint_summary(
    checkpoints: list[dict],
    horizons_seconds: tuple[float, ...],
    move_bands: tuple[float, ...],
) -> list[dict]:
    rows = []
    strategies = sorted({
        (str(item["strategy"]), str(item["state"]))
        for item in checkpoints
    })
    for strategy, state in strategies:
        subset = [
            item
            for item in checkpoints
            if item["strategy"] == strategy and item["state"] == state
        ]
        for horizon in horizons_seconds:
            horizon_key = f"{horizon:g}s"
            for band in move_bands:
                band_key = _band_label(band)
                counts = Counter(
                    (
                        item.get("forward", {})
                        .get(horizon_key, {})
                        .get("movementBands", {})
                        .get(band_key, {})
                        .get("classification", "no_data")
                    )
                    for item in subset
                )
                resolved = counts["hypothesis_first"] + counts["opposite_first"]
                rows.append({
                    "strategy": strategy,
                    "state": state,
                    "horizonSeconds": horizon,
                    "movePct": band * 100,
                    "total": len(subset),
                    "hypothesisFirst": counts["hypothesis_first"],
                    "oppositeFirst": counts["opposite_first"],
                    "ambiguous": counts["ambiguous"],
                    "unresolved": counts["unresolved"],
                    "noData": counts["no_data"],
                    "resolvedDirectionalRate": (
                        counts["hypothesis_first"] / resolved
                        if resolved > 0
                        else None
                    ),
                })
    return rows


def _overlap_summary(
    overlaps: list[dict],
    horizons_seconds: tuple[float, ...],
    move_bands: tuple[float, ...],
) -> list[dict]:
    rows = []
    groups = sorted({
        (
            tuple(item["strategyPair"]),
            str(item["relationship"]),
        )
        for item in overlaps
    })
    for pair, relationship in groups:
        subset = [
            item for item in overlaps
            if tuple(item["strategyPair"]) == pair
            and item["relationship"] == relationship
        ]
        for horizon in horizons_seconds:
            horizon_key = f"{horizon:g}s"
            for band in move_bands:
                band_key = _band_label(band)
                counts = Counter(
                    (
                        item.get("forward", {})
                        .get(horizon_key, {})
                        .get("movementBands", {})
                        .get(band_key, {})
                        .get("classification", "no_data")
                    )
                    for item in subset
                )
                rows.append({
                    "strategyPair": list(pair),
                    "relationship": relationship,
                    "horizonSeconds": horizon,
                    "movePct": band * 100,
                    "total": len(subset),
                    "upFirst": counts["up_first"],
                    "downFirst": counts["down_first"],
                    "ambiguous": counts["ambiguous"],
                    "unresolved": counts["unresolved"],
                    "noData": counts["no_data"],
                })
    return rows


def analyze_market_interactions(
    rows: list[dict],
    *,
    horizons_seconds: tuple[float, ...] = DEFAULT_HORIZONS_SECONDS,
    move_bands: tuple[float, ...] = DEFAULT_MOVE_BANDS,
    max_shared_level_distance_pct: float = DEFAULT_MAX_SHARED_LEVEL_DISTANCE_PCT,
) -> dict[str, Any]:
    horizons = tuple(sorted({
        float(value)
        for value in horizons_seconds
        if float(value) > 0
    }))
    bands = tuple(sorted({
        float(value)
        for value in move_bands
        if float(value) > 0
    }))
    series = _series_by_symbol(rows)

    latest_price: dict[str, float] = {}
    last_signature: dict[tuple[str, str], tuple | None] = {}
    active: dict[tuple[str, str], dict] = {}
    pair_signatures: dict[tuple[str, str, str], tuple | None] = {}
    checkpoints: list[dict] = []
    overlaps: list[dict] = []

    for index, row in enumerate(rows):
        event = str(row.get("event") or "")
        symbol = str(row.get("symbol") or "")
        ts = _row_ts(row)

        if event in {"research_frame", "market_frame"} and symbol:
            price = _sampled_price(row)
            if price is not None:
                latest_price[symbol] = price
            continue

        if event in {"symbol_activated", "symbol_deactivated"} and symbol:
            for strategy in TRACKED_STATES:
                last_signature.pop((symbol, strategy), None)
                active.pop((symbol, strategy), None)
            for pair_key in list(pair_signatures):
                if pair_key[0] == symbol:
                    pair_signatures.pop(pair_key, None)
            continue

        if event != "decision" or not symbol:
            continue

        decision = row.get("payload") or {}
        strategy = str(decision.get("strategy") or "")
        if strategy not in TRACKED_STATES:
            continue

        state = _state(decision)
        active_key = (symbol, strategy)
        if state not in TRACKED_STATES[strategy]:
            last_signature[active_key] = None
            active.pop(active_key, None)
            for pair_key in list(pair_signatures):
                if pair_key[0] == symbol and strategy in pair_key[1:]:
                    pair_signatures[pair_key] = None
            continue

        side = _hypothesis_side(decision)
        level = _reference_level(decision)
        object_id = _object_id(decision)
        action = str(decision.get("action") or "wait")
        signature = (state, object_id, action, side)
        if last_signature.get(active_key) == signature:
            continue
        previous = active.get(active_key)
        last_signature[active_key] = signature

        details = decision.get("details") or {}
        anchor = (
            details.get("currentPrice")
            or decision.get("entry")
            or latest_price.get(symbol)
            or level
        )
        if not isinstance(anchor, (int, float)) or anchor <= 0 or side is None:
            active.pop(active_key, None)
            continue
        anchor = float(anchor)

        checkpoint = {
            "checkpointId": f"interaction-{index}",
            "ts": ts,
            "symbol": symbol,
            "strategy": strategy,
            "hypothesis": HYPOTHESIS_KIND[strategy],
            "state": state,
            "transitionFrom": previous.get("state") if previous else None,
            "action": action,
            "hypothesisSide": side,
            "anchorPrice": anchor,
            "referenceLevel": level,
            "objectId": object_id,
            "confidence": decision.get("confidence"),
            "setupId": decision.get("setup_id") or decision.get("setupId"),
            "reasons": list(decision.get("reasons") or []),
            "features": _feature_snapshot(decision, anchor),
        }
        checkpoint["forward"] = _directional_forward(
            series.get(symbol),
            start_ts=ts,
            anchor_price=anchor,
            side=side,
            horizons_seconds=horizons,
            move_bands=bands,
        )
        checkpoints.append(checkpoint)
        active[active_key] = checkpoint

        # Density remains in checkpoint research, but it is no longer an
        # independent playbook after Stage 4. Conflict/confluence is therefore
        # measured only between tradeable playbooks.
        if strategy not in PLAYBOOK_STRATEGIES:
            continue

        for other_strategy in PLAYBOOK_STRATEGIES:
            if other_strategy == strategy:
                continue
            other = active.get((symbol, other_strategy))
            if other is None:
                continue
            other_level = other.get("referenceLevel")
            if (
                level is None
                or other_level is None
                or anchor <= 0
            ):
                continue
            level_distance = abs(float(level) - float(other_level)) / anchor
            if level_distance > max_shared_level_distance_pct:
                continue

            pair = tuple(sorted((strategy, other_strategy)))
            pair_key = (symbol, pair[0], pair[1])
            left = checkpoint if checkpoint["strategy"] == pair[0] else other
            right = checkpoint if checkpoint["strategy"] == pair[1] else other
            relationship = (
                "confluence"
                if left["hypothesisSide"] == right["hypothesisSide"]
                else "conflict"
            )
            pair_signature = (
                left["state"],
                left["objectId"],
                left["hypothesisSide"],
                right["state"],
                right["objectId"],
                right["hypothesisSide"],
                relationship,
            )
            if pair_signatures.get(pair_key) == pair_signature:
                continue
            pair_signatures[pair_key] = pair_signature

            overlap = {
                "overlapId": f"overlap-{len(overlaps) + 1}",
                "ts": ts,
                "symbol": symbol,
                "strategyPair": list(pair),
                "relationship": relationship,
                "anchorPrice": anchor,
                "levelDistancePct": level_distance,
                "left": {
                    key: left.get(key)
                    for key in (
                        "strategy",
                        "state",
                        "hypothesis",
                        "hypothesisSide",
                        "referenceLevel",
                        "objectId",
                    )
                },
                "right": {
                    key: right.get(key)
                    for key in (
                        "strategy",
                        "state",
                        "hypothesis",
                        "hypothesisSide",
                        "referenceLevel",
                        "objectId",
                    )
                },
            }
            overlap["forward"] = _neutral_forward(
                series.get(symbol),
                start_ts=ts,
                anchor_price=anchor,
                horizons_seconds=horizons,
                move_bands=bands,
            )
            if relationship == "conflict":
                sides = {
                    left["hypothesisSide"]: left["strategy"],
                    right["hypothesisSide"]: right["strategy"],
                }
                for horizon_row in overlap["forward"].values():
                    for band_row in horizon_row.get("movementBands", {}).values():
                        classification = band_row.get("classification")
                        if classification == "up_first":
                            winner = sides.get("long")
                        elif classification == "down_first":
                            winner = sides.get("short")
                        else:
                            winner = None
                        band_row["winnerStrategy"] = winner
            overlaps.append(overlap)

    state_summary = _checkpoint_summary(checkpoints, horizons, bands)
    overlap_summary = _overlap_summary(overlaps, horizons, bands)

    return {
        "schemaVersion": 1,
        "priceSampling": (
            "research/market frame lastPrice (candle close fallback); "
            "OHLC highs/lows are intentionally not used because repeated "
            "forming-candle extremes may predate a checkpoint"
        ),
        "horizonsSeconds": list(horizons),
        "movementBandsPct": [band * 100 for band in bands],
        "maxSharedLevelDistancePct": max_shared_level_distance_pct,
        "summary": {
            "checkpoints": len(checkpoints),
            "overlaps": len(overlaps),
            "conflicts": sum(
                item["relationship"] == "conflict"
                for item in overlaps
            ),
            "confluences": sum(
                item["relationship"] == "confluence"
                for item in overlaps
            ),
            "liquidityEvidenceCheckpoints": sum(
                item["strategy"] == "orderbook_density"
                for item in checkpoints
            ),
        },
        "strategyStateSummary": state_summary,
        "overlapSummary": overlap_summary,
        "checkpoints": checkpoints,
        "overlaps": overlaps,
    }
