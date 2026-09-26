"""Causal, single-owner routing before playbook evaluation.

No orders, portfolio, PnL fitting or independent indicator pipeline lives here.
Scores are categorical priorities, never estimated probabilities.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from statistics import median

from .domain import Action, StrategyDecision, Trend
from .strategy.price_action_hypothesis import PriceActionHypothesisStrategy
from .strategy.structure import directional_level_kind
from .strategy.breakout import LevelBreakoutStrategy
from .strategy.weak_level_rejection import WeakLevelRejectionStrategy
from .strategy.scenario_objects import (
    level_object_id, trendline_object_id, candle_object_id, decision_object_id,
)
from .scenario_episodes import FailedBreakEpisodes


ROLES = {
    "level_breakout": "structural_break",
    "weak_level_rejection": "failed_break_reclaim",
    "trend_structure": "directional_pullback",
    "orderbook_density": "evidence_only",
    "price_action_hypothesis": "closed_candle_pattern_beta",
}
EVENTS = {
    "level_breakout": "executions and executable quote beyond boundary, or retest response",
    "weak_level_rejection": "failed break / reclaim with absorption and executable response",
    "trend_structure": "pullback test, reclaim and directional executions",
    "price_action_hypothesis": "closed pattern extreme crossed with volume and aligned flow",
}
TERMINAL = {"COMPLETED", "INVALIDATED", "EXPIRED", "RELEASED"}


@dataclass
class Scenario:
    symbol: str
    scenario_id: str
    owner: str
    side: str
    signature: str
    anchor: float
    range_abs: float
    observed_mono: float
    assigned_mono: float
    expires_mono: float
    reasons: list[str]
    level: dict = field(default_factory=dict)
    state: str = "ASSIGNED"
    setup_id: str | None = None
    prepared_mono: float | None = None
    first_signal_mono: float | None = None
    admitted_mono: float | None = None
    sent_mono: float | None = None
    filled_mono: float | None = None
    ended_mono: float | None = None
    frozen: dict | None = None
    preview: dict | None = None
    last_rejection: dict | None = None
    cancellation: str | None = None
    last_transition_reason: str | None = None
    version: int = 1
    candidate_signatures: tuple[str, ...] = ()  # legacy serialized contracts
    object_id: str | None = None
    market_basis: str | None = None

    def public(self):
        return dict(schemaVersion=1, scenarioId=self.scenario_id, owner=self.owner,
            marketObjectId=self.object_id, marketBasis=self.market_basis,
            side=self.side, state=self.state, generation=self.signature, setupId=self.setup_id,
            planVersion=self.version, reasons=list(self.reasons), anchor=self.anchor,
            movementBudget=self.frozen["budget"] if self.frozen else self.range_abs * 2, expectedEvent=EVENTS.get(self.owner, "legacy position protection"),
            invalidation="structural invalidation, departure from entry area or expiry; no risk reset",
            expiresMono=self.expires_mono, level=deepcopy(self.level),
            entryArea=(deepcopy(self.frozen["entryArea"]) if self.frozen else
                       [self.anchor-self.range_abs, self.anchor+self.range_abs]),
            times=dict(observed=self.observed_mono, assigned=self.assigned_mono, prepared=self.prepared_mono,
                firstSignal=self.first_signal_mono, admitted=self.admitted_mono,
                orderSent=self.sent_mono, fill=self.filled_mono, completed=self.ended_mono,
                clock="monotonic"), lastRejection=deepcopy(self.last_rejection),
            preparation=deepcopy(self.preview), cancellation=self.cancellation,
            lastTransitionReason=self.last_transition_reason)


class ScenarioRouter:
    def __init__(self, *, preparation_seconds=300.0, signal_seconds=60.0):
        self.preparation_seconds = preparation_seconds
        self.signal_seconds = signal_seconds
        self.scenarios: dict[str, Scenario] = {}
        self.situations: dict[str, dict] = {}
        self._generation: dict[str, int] = {}
        self._consumed: dict[str, dict[str, tuple[float, float]]] = {}
        self._transitions: list[tuple[str, dict]] = []
        self._recent = {}
        self._episodes: dict[str, FailedBreakEpisodes] = {}

    def transition(self, s, state, now, reason):
        if s.state == state:
            return
        previous = s.state
        s.state = state
        s.last_transition_reason = reason
        if state == "PREPARED" and s.prepared_mono is None:
            s.prepared_mono = now
        if state in TERMINAL:
            s.ended_mono = now
            # Consume this market episode, not every candidate seen beside it.
            # Also prevent changing the owner/direction as a risk-retry fallback
            # on the SAME object. A new actual failed break has a distinct basis.
            keys = {s.market_basis or s.signature}
            if s.object_id:
                keys.add(f"{s.object_id}:approach")
            for key in keys:
                self._consumed.setdefault(s.symbol, {})[key] = (s.anchor, s.range_abs)
        self._transitions.append((s.symbol, dict(s.public(), previousState=previous,
            transitionMono=now, transitionReason=reason)))

    def drain(self):
        events, self._transitions = self._transitions, []
        return events

    def public(self, symbol):
        s = self.scenarios.get(symbol)
        return dict(situation=deepcopy(self.situations.get(symbol, {
            "status": "INSUFFICIENT_DATA", "reason": "bootstrap not ready"})),
            scenario=s.public() if s else None)

    def restore_execution(self, symbol, plan, now):
        """A late fill carries its ORIGINAL contract, even after cancel/new assignment."""
        saved = (getattr(plan, "strategy_details", None) or {}).get("scenario", {})
        current = self.scenarios.get(symbol)
        if current and current.owner == plan.strategy and (
                not saved or current.scenario_id == saved.get("scenarioId")):
            return current
        if current and current.state not in TERMINAL:
            self.transition(current, "INVALIDATED", now, "late fill restored original execution owner")
        anchor = getattr(plan, "entry", getattr(plan, "market_entry", 0))
        s = Scenario(symbol, saved.get("scenarioId", f"{symbol}:recovered"), plan.strategy,
            plan.side.value, saved.get("generation", str(plan.setup_id)),
            saved.get("anchor", anchor), max(saved.get("movementBudget", 0)/2, 1e-12),
            now, now, now+self.preparation_seconds, ["restored execution owner"],
            setup_id=plan.setup_id, level=deepcopy(saved.get("level") or {}),
            object_id=saved.get("marketObjectId"), market_basis=saved.get("marketBasis"))
        self.scenarios[symbol] = s
        return s

    def observe(self, symbol, context, candles, structure, enabled, now, *, position=None, pending=None):
        """Evaluate applicability, not already-fired strategy signals."""
        episodes = self._episodes.setdefault(symbol, FailedBreakEpisodes())
        situation, candidates = self.assess(context, candles, structure, enabled, episodes=episodes)
        samples = self._recent.setdefault(symbol, deque(maxlen=120))
        if context is not None:
            fast_flow = context.flow.horizons.get(5) if context.flow else None
            notional = fast_flow.trade_notional_usd if fast_flow else None
            baseline_spread = median(x[1] for x in samples) if samples else None
            baseline_flow = median(x[2] for x in samples if x[2] is not None) if any(x[2] is not None for x in samples) else None
            situation["recentNormalization"] = dict(
                samples=len(samples), spreadRatio=context.execution.spread_pct/baseline_spread if baseline_spread else None,
                flowActivityRatio=notional/baseline_flow if notional is not None and baseline_flow else None,
                fallback="unavailable baseline stays null; no post-signal calibration wait")
            if not samples or context.observed_at_ms-samples[-1][0]>=1000:
                samples.append((context.observed_at_ms, context.execution.spread_pct, notional))
        self.situations[symbol] = situation
        s = self.scenarios.get(symbol)
        # Execution owns the cancellation/fill race. Never reassign before its ack.
        if position is not None or pending is not None:
            owner = position.strategy if position is not None else pending.plan.strategy
            plan = position if position is not None else pending.plan
            s = self.restore_execution(symbol, plan, now)
            self.transition(s, "IN_POSITION" if position is not None else "ORDER_PENDING", now,
                            "execution ownership is immutable")
            if pending is not None and not enabled.get(owner, False):
                s.cancellation = "strategy_disabled"
            return s
        price = context.last_price if context else 0
        consumed = self._consumed.setdefault(symbol, {})
        # A new approach after a real departure is a new market basis. A target,
        # score or wall-clock change alone never rearms a consumed opportunity.
        for key, (anchor, span) in list(consumed.items()):
            if price > 0 and abs(price-anchor) > 3*span:
                del consumed[key]
        if s and s.state not in TERMINAL:
            reason = None
            if not enabled.get(s.owner, False):
                reason = "strategy_disabled"
            elif s.object_id and s.owner in {"level_breakout", "weak_level_rejection"} and not any(
                    level_object_id(level) == s.object_id and level.lifecycle != "broken"
                    for level in (structure.levels if structure else [])):
                reason = "assigned_market_object_unavailable"
            elif s.object_id and s.owner == "trend_structure" and not any(
                    trendline_object_id(line) == s.object_id
                    for line in (structure.trendlines if structure else [])):
                reason = "assigned_market_object_unavailable"
            elif now >= s.expires_mono:
                reason = "scenario_expired"
            elif price > 0 and abs(price-s.anchor) > 3*s.range_abs:
                reason = "price_left_preparation_area"
            elif s.owner == "level_breakout" and any(
                    c["owner"] == "weak_level_rejection" and c["priority"] == 90
                    and c["side"] != s.side and abs(c["anchor"]-s.anchor) < s.range_abs*.1
                    for c in candidates):
                reason = "observed_failed_break_changed_market_basis"
            elif s.owner == "price_action_hypothesis" and candidates and all(
                    c["signature"] != s.signature for c in candidates):
                reason = "closed_pattern_replaced"
            if reason:
                self.transition(s, "EXPIRED" if reason == "scenario_expired" else "INVALIDATED", now, reason)
                return None  # Reassess at the NEXT market observation, not risk fallback.
            return s  # No score-based churn while the market hypothesis still holds.
        eligible = [c for c in candidates if c["marketBasis"] not in consumed]
        if not eligible:
            if situation["status"] != "INSUFFICIENT_DATA":
                situation["status"] = "NO_SUITABLE_SCENARIO"
                situation["reason"] = "no eligible unconsumed market scenario"
            return None
        choice = max(eligible, key=lambda c: (c["priority"], -c["distance"], c["signature"]))
        n = self._generation.get(symbol, 0)+1
        self._generation[symbol] = n
        s = Scenario(symbol, f"{symbol}:scenario:{n}", choice["owner"], choice["side"],
            choice["signature"], choice["anchor"], situation["rangeAbs"], now, now,
            now+self.preparation_seconds, choice["reasons"], level=choice.get("level", {}),
            state="OBSERVING", object_id=choice["objectId"], market_basis=choice["marketBasis"])
        self.scenarios[symbol] = s
        self.transition(s, "ASSIGNED", now, "applicable market scenario selected before signal")
        return s

    @staticmethod
    def assess(context, candles, structure, enabled, *, episodes=None):
        rows = [c for c in candles if c.confirmed and context is not None
                and c.start_ms+60_000 <= context.observed_at_ms]
        availability = {key: dict(role=role, status="disabled" if not enabled.get(key, False)
            else "evidence_only" if role == "evidence_only" else "not_applicable",
            reason="not enabled" if not enabled.get(key, False) else "scenario absent")
            for key, role in ROLES.items()}
        result = dict(status="INSUFFICIENT_DATA", strategies=availability,
            reason="need fresh execution context and at least 21 closed 1m bars")
        if context is None or not context.execution.ready or context.execution.book_synced is False or len(rows)<21 or not context.last_price:
            for key, row in availability.items():
                if row["status"] not in {"disabled", "evidence_only"}:
                    row.update(status="insufficient_data", reason=result["reason"])
            return result, []
        for key, row in availability.items():
            minimum = 21 if key == "price_action_hypothesis" else 40
            if row["status"] not in {"disabled", "evidence_only"} and len(rows)<minimum:
                row.update(status="insufficient_data", reason=f"need {minimum} closed bars")
        span = median(c.high-c.low for c in rows[-20:])
        if span <= 0:
            result["reason"] = "observed range has no positive scale"
            return result, []
        price = context.last_price
        local = context.local_regime
        regime = local.regime.value if local else "unknown"
        parent = local.parent_direction if local else Trend.FLAT
        direction = local.direction if local else Trend.FLAT
        volume_base = median(c.volume for c in rows[-21:-1])
        result.update(status="OBSERVING", reason="causal instrument-relative features available",
            regime=regime, rangeAbs=span, spreadToRange=context.execution.spread_pct*price/span,
            volumeRatio=rows[-1].volume/volume_base if volume_base else None,
            rangeExpansion=(rows[-1].high-rows[-1].low)/span,
            normalization="median of preceding closed bars; no future PnL",
            flow=context.flow.public() if context.flow else None,
            liquidity=context.liquidity.public() if context.liquidity else None)
        candidates = []
        def add(owner, side, anchor, key, priority, reason, level=None, *, object_id=None, episode=None):
            if object_id is None:
                return
            if not enabled.get(owner, False):
                return
            minimum = 21 if owner == "price_action_hypothesis" else 40
            if len(rows)<minimum:
                availability[owner].update(status="insufficient_data", reason=f"need {minimum} closed bars")
                return
            alignment = context.flow_alignment_for(Action(side))
            liquidity = context.liquidity_alignment_for(Action(side))
            obstacles = [abs(x.center-price)/span for x in (structure.levels if structure else [])
                         if ((x.center>price and directional_level_kind(x.kind)=="resistance") if side=="long"
                             else (x.center<price and directional_level_kind(x.kind)=="support"))]
            availability[owner].update(status="applicable", reason=reason,
                flow=alignment.classification.value if alignment else "unavailable",
                liquidity=liquidity.classification.value if liquidity else "unavailable",
                obstacleDistanceInRanges=min(obstacles) if obstacles else None)
            candidates.append(dict(owner=owner, side=side, anchor=anchor,
                signature=f"{owner}:{side}:{key}", priority=priority,
                distance=abs(price-anchor)/span, reasons=[reason], level=level or {},
                objectId=object_id, marketBasis=f"{object_id}:{episode or 'approach'}"))
        # One near structural object, with candle/flow evidence for routing only;
        # the assigned playbook must still observe its own entry event.
        for level in (structure.levels if structure else []):
            kind = directional_level_kind(level.kind)
            if kind is None or abs(price-level.center)>2*span:
                continue
            side = "long" if kind == "support" else "short"
            last = rows[-1]
            forming = context.forming_candle
            object_id = level_object_id(level)
            if object_id is None or level.lifecycle == "broken":
                continue
            tracker = episodes if episodes is not None else FailedBreakEpisodes()
            failed_event = tracker.observe(object_id, level, side, price,
                context.observed_at_ms, [last, forming])
            failed = failed_event is not None
            key = level.generation_id
            if ((failed or regime in {"range", "transition", "unclear"})
                    and WeakLevelRejectionStrategy._structural_level_is_tradeable(level, price, kind)):
                rejection_key = f"{key}:failed:{failed_event}" if failed else key
                add("weak_level_rejection", side, level.center, rejection_key, 90 if failed else 50,
                    "failed break/reclaim" if failed else "range boundary reaction", level.public(),
                    object_id=object_id, episode=f"failed:{failed_event}" if failed else None)
            break_side = "short" if side=="long" else "long"
            break_direction = Trend.DOWN if break_side=="short" else Trend.UP
            if (not failed
                    and LevelBreakoutStrategy._structural_level_is_tradeable(
                        level, rows, long_candidate=break_side == "long")
                    and LevelBreakoutStrategy._select_structural_candidate(
                        [level], price, long_side=break_side == "long") is not None):
                # Local direction/flow is a preference, never an hourly veto on
                # an actual boundary break in the opposite direction.
                preference = int(direction == break_direction) + int(
                    context.flow is not None and context.flow.dominant_direction == break_direction)
                add("level_breakout", break_side, level.center, key, 70+preference,
                    "approach to structural boundary / expansion", level.public(), object_id=object_id)
        if regime != "pullback":
            parent = direction
        if regime in {"pullback","bullish_trend","bearish_trend"} and parent in {Trend.UP, Trend.DOWN} and structure:
            lines = [line for line in structure.trendlines
                     if line.kind == ("support" if parent==Trend.UP else "resistance")]
            for line in lines:
                if (line.touches >= 3 and (line.slope_per_bar > 0 if parent == Trend.UP else line.slope_per_bar < 0)
                        and abs(price-line.current_price)<=2*span):
                    add("trend_structure", "long" if parent==Trend.UP else "short", line.current_price,
                        f"{line.kind}:{line.start_ms}:{line.end_ms}", 80,
                        "directional pullback at existing trendline", line.public(),
                        object_id=trendline_object_id(line))
        # Beta is a named closed-candle pattern, never a catch-all fallback.
        htf = context.htf_bias
        if htf and local and regime != "range":
            trend = htf.trend_1h if htf.trend_1h!=Trend.FLAT else htf.trend_15m
            sign = 1 if trend==Trend.UP else -1
            opposite = Trend.DOWN if sign==1 else Trend.UP
            if trend!=Trend.FLAT and htf.trend_15m!=opposite and local.structure_5m!=opposite and direction!=opposite:
                pattern = PriceActionHypothesisStrategy._pattern(rows[-1], rows[-2], sign)
                if pattern and result["volumeRatio"] is not None and result["volumeRatio"]>=1.2:
                    add("price_action_hypothesis", "long" if sign==1 else "short", rows[-1].close,
                        str(rows[-1].start_ms), 60, f"closed candle {pattern} with relative volume",
                        object_id=candle_object_id(rows[-1].start_ms))
        if not candidates:
            insufficient = any(row["status"]=="insufficient_data" for row in availability.values())
            result["status"]="INSUFFICIENT_DATA" if insufficient else "NO_SUITABLE_SCENARIO"
            if insufficient:
                result["reason"]="enabled playbooks need more closed history"
        return result, candidates

    def accept_decision(self, symbol, decision, now, book):
        s = self.scenarios.get(symbol)
        if s is None or s.state in TERMINAL or decision.strategy != s.owner:
            return StrategyDecision(decision.strategy, Action.WAIT, ["scenario not owned"], details={"state":"not_assigned"})
        if s.state in {"IN_POSITION", "ORDER_PENDING"}:
            decision.details["scenario"] = s.public()
            return decision
        if not decision.tradeable:
            if decision.details.get("state") in {"armed","break","test","reclaim","pullback","reject"}:
                self.transition(s,"PREPARED",now,"owner preparing its causal event")
            decision.details["scenario"] = s.public()
            return decision
        def reject(reason):
            s.last_rejection=dict(owner="scenario", reason=reason, atMono=now)
            self.transition(s,"EXPIRED" if "expired" in reason else "INVALIDATED",now,reason)
            return StrategyDecision(s.owner, Action.WAIT, [reason], details={"state":"scenario_invalidated","scenario":s.public()})
        if decision.action.value != s.side:
            return reject("owner changed hypothesis direction")
        if s.object_id is not None and decision_object_id(decision) != s.object_id:
            return reject("owner plan does not match assigned market object")
        if s.frozen is None:
            if s.prepared_mono is None:
                s.prepared_mono = now
            s.first_signal_mono = now
            s.setup_id = decision.setup_id
            # Freeze structure and opportunity separately from the FIRE clock.
            # A risk refusal cannot reset any of these fields on the next callback.
            budget = 2*s.range_abs
            entry = float(decision.entry)
            original = decision.details.get("opportunityTrigger") or decision.details.get("opportunityArm") or {}
            opportunity_price = float(original.get("price") or s.anchor)
            explicit = original.get("expectedImpulsePct")
            if isinstance(explicit,(int,float)) and explicit > 0:
                budget = min(budget, opportunity_price*explicit)
            s.frozen = dict(entry=entry, stop=decision.stop, target=decision.target,
                watched_level=decision.watched_level,
                entryArea=[entry-budget*.25,entry+budget*.25],
                details={k:deepcopy(decision.details[k]) for k in (
                    "zone","levelLifecycle","levelGeneration","zoneGeneration",
                    "trendline","trendlineAnchor","hypothesis","opportunityTrigger","opportunityArm","fireTrigger",
                    "preparedOpportunity","entryContextAssessment","targetSource","allowRunner")
                    if k in decision.details}, budget=budget,
                opportunityPrice=opportunity_price,
                opportunityMs=int(original.get("observedAtMs") or 0))
            s.expires_mono = min(s.expires_mono, now+self.signal_seconds)
        if now>=s.expires_mono:
            return reject("ready scenario expired")
        executable = book.executable_entry(decision.side)
        low, high = s.frozen["entryArea"]
        if executable is None or not low<=executable<=high:
            return reject("price left frozen entry area; no chase after risk refusal")
        for key in ("entry","stop","target","watched_level"):
            setattr(decision,key,s.frozen[key])
        decision.setup_id=s.setup_id
        decision.details.update(deepcopy(s.frozen["details"]))
        decision.details["expectedImpulsePct"] = s.frozen["budget"]/s.frozen["opportunityPrice"]
        decision.details["opportunityTrigger"] = dict(price=s.frozen["opportunityPrice"],
            observedAtMs=s.frozen["opportunityMs"],
            expectedImpulsePct=decision.details["expectedImpulsePct"], source="scenario_frozen_budget")
        self.transition(s,"ARMED",now,"owner supplied frozen executable plan")
        decision.details["scenario"] = s.public()
        decision.details["planContractVersion"] = 1
        return decision

    def reject(self, symbol, owner, reason, now):
        s=self.scenarios.get(symbol)
        if s:
            s.last_rejection=dict(owner=owner,reason=reason,atMono=now)

    def submitted(self, symbol, now):
        s=self.scenarios[symbol]
        s.admitted_mono=now; s.sent_mono=now
        self.transition(s,"ORDER_PENDING",now,"final risk and execution checks passed")

    def filled(self, symbol, now):
        s=self.scenarios.get(symbol)
        if s:
            s.filled_mono=now
            self.transition(s,"IN_POSITION",now,"fill belongs to original order owner")

    def cancelled(self, symbol, now, reason, *, setup_id=None, scenario_id=None):
        s=self.scenarios.get(symbol)
        if s and ((scenario_id and scenario_id != s.scenario_id)
                  or (setup_id and s.setup_id and setup_id != s.setup_id)):
            return  # A delayed acknowledgement cannot cancel a newer order.
        if s and s.state != "IN_POSITION":
            s.cancellation=reason
            self.transition(s,"INVALIDATED",now,"cancellation acknowledged; reassess on next event")

    def completed(self, symbol, now, reason):
        s=self.scenarios.get(symbol)
        if s:
            self.transition(s,"COMPLETED",now,reason)
