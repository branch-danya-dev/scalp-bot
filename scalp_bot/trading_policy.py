"""Deterministic trading-v1 policy. Thresholds are safety choices, not fitted optima.

Runs at every economic-plan attempt, after the scenario has frozen its causal
anchor and before sizing. External venues have entry authority only.
"""
from dataclasses import replace
from math import isfinite
from statistics import median

from .domain import Trend
from .strategy.common import compute_trade_flow
from .strategy.semantic_arbiter import assess_structural_path


def number(value, default=0.0):
    return float(value) if type(value) in (int, float) and isfinite(value) else default


def local_direction(context):
    local = context.local_regime if context else None
    if local is None:
        return Trend.FLAT
    if local.regime.value in {"bullish_impulse", "bearish_impulse", "bullish_trend", "bearish_trend"}:
        return local.direction
    if local.regime.value == "pullback":
        return local.parent_direction
    return Trend.FLAT


def rejection_classification(context, side):
    direction = local_direction(context)
    if direction == Trend.FLAT:
        return "range_rejection"
    aligned = direction == (Trend.UP if side == "long" else Trend.DOWN)
    return "trend_aligned_rejection" if aligned else "countertrend_reaction"


def cross_classification(snapshot, side, config):
    """Both fresh venues must agree in price AND executed flow. Missing is neutral."""
    sign = 1 if side == "long" else -1
    votes = []
    for venue in ("binance", "okx"):
        row = (snapshot or {}).get("venues", {}).get(venue, {})
        if not row.get("available") or number(row.get("ageMs"), 1e9) > 1500:
            return "unavailable"
        move = number(row.get("returnsBps", {}).get("1000"))
        flow = number(row.get("tradeImpulse"))
        if (abs(move) < config.cross_venue_min_move_bps or abs(flow) < .15
                or move * flow <= 0 or number(row.get("tradeVolumeUsd")) <= 0):
            votes.append(0)
        else:
            votes.append(1 if move * sign > 0 else -1)
    return "aligned" if votes == [1, 1] else "opposed" if votes == [-1, -1] else "neutral"


def participation_quality(flow, candles, forming, side, config):
    sign = 1 if side == "long" else -1
    turnover = [c.turnover for c in candles[-20:] if c.confirmed and c.turnover > 0]
    baseline = median(turnover) / 12 if turnover else 0.0
    notional = number(flow.get("notional5s"))
    relative = notional / baseline if baseline > 0 else 0.0
    pace = number(getattr(forming, "volume_pace_ratio", None), -1)
    response = sign * number(flow.get("priceMove5sPct")) * 10000
    reasons = []
    if not flow.get("baselineReady") or baseline <= 0:
        reasons.append("participation_baseline_missing")
    if relative < config.participation_min_notional_ratio:
        reasons.append("executed_notional_below_baseline")
    if number(flow.get("acceleration")) < 1 or number(flow.get("tradeRateRatio")) < config.participation_min_trade_rate_ratio:
        reasons.append("insufficient_trade_intensity")
    if number(flow.get("tradeCount5s")) < 3:
        reasons.append("insufficient_trade_count")
    if pace >= 0 and pace < config.participation_min_volume_pace:
        reasons.append("candle_volume_pace_weak")
    if sign * number(flow.get("imbalance5s")) < .03 or response < config.participation_min_response_bps:
        reasons.append("directional_price_response_missing")
    return dict(confirmed=not reasons, reasons=reasons, notional5s=notional,
                baselineNotional5s=baseline, notionalRatio=relative,
                tradeCount5s=flow.get("tradeCount5s", 0), tradeRateRatio=flow.get("tradeRateRatio", 0),
                acceleration=flow.get("acceleration", 0), candleVolumePace=pace if pace >= 0 else None,
                imbalance5s=flow.get("imbalance5s", 0), directionalResponseBps=response)


def prepare(engine, session, decision):
    config = engine.config
    if not config.trading_quality_enabled:
        return None
    details = decision.details
    context = session.market_context
    side = decision.side.value
    direction = 1 if side == "long" else -1
    trend = local_direction(context)
    aligned = trend == (Trend.UP if side == "long" else Trend.DOWN)
    details["tradingQualityPolicy"] = "trading-v1"
    details["marketTargetOnly"] = True
    details["alignmentPriority"] = 2 if aligned else 1 if trend == Trend.FLAT else 0
    executable = session.orderbook.executable_entry(decision.side)
    if executable:
        reachable_target(decision, executable, float(decision.target),
                         tick_size=getattr(getattr(session, "instrument", None), "tick_size", 0.0))
    snapshot = engine.cross.snapshot(session.symbol, engine.clock.perf_counter_ns()) if hasattr(engine, "cross") else {}
    cross = cross_classification(snapshot, side, config)
    details["crossVenueContext"] = dict(classification=cross, mode="deterministic_entry_only",
                                          asOfMonoNs=snapshot.get("asOfMonoNs"))
    details["crossVenuePriority"] = int(cross == "aligned")
    stamp = context.observed_at_ms if context else int(engine.clock.time() * 1000)
    flow = compute_trade_flow(list(session.trades), stamp)
    quality = participation_quality(flow, list(session.candles), context.forming_candle if context else None, side, config)
    details["participationQuality"] = quality
    counter = False
    preview = details.get("preparationOnly") is True
    if decision.strategy == "weak_level_rejection":
        kind = rejection_classification(context, side)
        details["rejectionClass"] = kind
        counter = kind == "countertrend_reaction"
        if counter:
            details["allowRunner"] = False
            details["exitMode"] = "short_reaction"
            details["noFollowThroughSeconds"] = config.countertrend_no_follow_through_seconds
            details["countertrendFailureSeconds"] = config.countertrend_failure_seconds
            anchor = details.get("opportunityTrigger") or {}
            budget = number(anchor.get("expectedImpulsePct")) * number(anchor.get("price"))
            # Scenario budget is two typical ranges; reaction uses at most one.
            details["reactionBudget"] = budget * config.countertrend_reaction_budget_fraction
            if not preview and not episode_absorption_confirmed(details, stamp):
                return "countertrend_requires_absorption_and_immediate_response"
            if not preview and broader_continuation_opposed(context, side):
                return "countertrend_flow_continues_against_reaction"
    if decision.strategy == "trend_structure" and not aligned:
        return "trend_structure_requires_parent_trend_alignment"
    if decision.strategy == "level_breakout":
        if trend != Trend.FLAT and not aligned:
            return "countertrend_breakout_forbidden"
        # This supplements the existing 1h_only override, without weakening it.
        path = breakout_path(decision, context, config, executable=executable)
        details["reachableStructuralPath"] = path.public()
        details["reachableObstaclePrice"] = (path.obstacle.get("low" if side == "long" else "high")
                                              if path.obstacle and not path.own_breakout_level_exempted else None)
        if path.obstacle_before_first_take and not path.own_breakout_level_exempted:
            return "strong_obstacle_before_first_take"
    if not preview and (decision.strategy == "level_breakout" or counter):
        if not quality["confirmed"]:
            return "participation_quality:" + ",".join(quality["reasons"])
        if cross == "opposed":
            return "fresh_cross_venue_continuation_opposed"
    return None


def breakout_path(decision, context, config, *, executable=None, plan=None):
    """Check the actual first exit; a notional 1R beyond the target is not an exit."""
    entry = plan.market_entry if plan else executable or decision.entry
    target = plan.target if plan else (decision.details.get("remainingMove") or {}).get("reachableTarget", decision.target)
    details = dict(decision.details)
    sign = 1 if decision.side.value == "long" else -1
    if plan:
        first = plan.strategy_details["economics"]["firstTakePrice"]
    else:
        distance = max(0.0, sign * (target - entry))
        if config.partial_take_enabled and details.get("allowRunner", True):
            distance = min(distance, abs(entry - decision.stop) * max(0.0, config.partial_take_at_r))
        first = entry + sign * distance
    details.update(plannedEntryPrice=entry, plannedFirstTakePrice=first)
    priced = replace(decision, entry=entry, target=target, details=details)
    return assess_structural_path(priced, context, partial_take_at_r=config.partial_take_at_r,
        partial_take_enabled=config.partial_take_enabled, skip_accepted_breakout_level=True)


def episode_absorption_confirmed(details, observed_at_ms):
    """Absorption precedes response; its instantaneous flow flag may turn off."""
    if not details.get("microResponseReady"):
        return False
    if details.get("attackAbsorbed"):
        return True
    evidence = details.get("absorptionEvidence") or {}
    episode = (details.get("scenario") or {}).get("episodeKey")
    generation = details.get("levelGeneration")
    if (not episode or evidence.get("episodeKey") != episode
            or not generation or evidence.get("generation") != generation):
        return False
    age = (observed_at_ms - number(evidence.get("observedAtMs"))) / 1000
    return (number(evidence.get("observedAtMs")) > 0
            and 0 <= age <= number(evidence.get("responseWindowSeconds")))


def broader_continuation_opposed(context, side):
    if context is None or context.flow is None:
        return False
    sign = 1 if side == "long" else -1
    # Broader executed continuation must have turned, not merely a brief 5s flip.
    horizons = context.flow.horizons
    return all(h in horizons and horizons[h].trade_count >= 3
               and sign * horizons[h].trade_imbalance < -.15 for h in (15, 60))


def reachable_target(decision, executable, target, *, tick_size=0.0):
    """Absolute frozen price ceiling: recomputation cannot renew spent movement."""
    details = decision.details
    sign = 1 if decision.side.value == "long" else -1
    trigger = details.get("opportunityTrigger") or {}
    anchor = number(trigger.get("price"))
    impulse = number(trigger.get("expectedImpulsePct"))
    if anchor <= 0 or not 0 < impulse < 1 or number(trigger.get("observedAtMs")) <= 0:
        return target, "causal_movement_anchor_missing"
    total = anchor * impulse
    if details.get("rejectionClass") == "countertrend_reaction":
        total = min(total, number(details.get("reactionBudget")))
    spent = max(0.0, sign * (executable - anchor))
    remaining = max(total - spent, 0.0)
    ceiling = executable + sign * remaining
    target = min(target, ceiling) if sign > 0 else max(target, ceiling)
    obstacle = number(details.get("reachableObstaclePrice"))
    tick = max(0.0, number(tick_size))
    if obstacle > 0:
        boundary = obstacle - sign * tick
        target = min(target, boundary) if sign > 0 else max(target, boundary)
    for row in details.get("liquidityLadder") or []:
        price = number(row.get("price"))
        if sign * (price - executable) > 0:
            boundary = price - sign * tick
            target = min(target, boundary) if sign > 0 else max(target, boundary)
    reason = "remaining_move_exhausted" if remaining <= 0 or sign * (target - executable) <= 0 else None
    details["remainingMove"] = dict(triggerPrice=anchor, triggerTimestamp=trigger["observedAtMs"],
        executableEntry=executable, expectedImpulseBps=impulse * 10000,
        budgetBps=total / anchor * 10000, spentBps=spent / anchor * 10000,
        remainingBps=remaining / anchor * 10000, reachableTarget=target, rejectReason=reason)
    return target, reason
