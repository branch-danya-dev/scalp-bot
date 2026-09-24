from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from ..domain import Action, StrategyDecision, TradePlan
from .market_context import MarketContext
from .structure import StructuralLevel


PLAYBOOK_KEYS = {
    "trend_structure",
    "weak_level_rejection",
    "level_breakout",
}

FLOW_PRIORITY = {
    "strongly_aligned": 4,
    "aligned": 3,
    "mixed": 2,
    "insufficient_data": 1,
    "short_term_reversal": 0,
    "opposed": -1,
}

LIQUIDITY_PRIORITY = {
    "supportive": 2,
    "neutral": 1,
    "unknown": 0,
    "opposed": -1,
}

FRESHNESS_PRIORITY = {
    "fresh": 4,
    "acceptable": 3,
    "unknown": 2,
    "late": 1,
    "exhausted": 0,
}


@dataclass(frozen=True, slots=True)
class StructuralPathAssessment:
    blocked: bool
    side: str
    first_take_price: float | None
    first_take_distance_pct: float | None
    obstacle: dict[str, Any] | None
    obstacle_distance_pct: float | None
    obstacle_before_first_take: bool
    own_breakout_level_exempted: bool
    risk_scale: float
    reasons: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "side": self.side,
            "firstTakePrice": self.first_take_price,
            "firstTakeDistancePct": self.first_take_distance_pct,
            "obstacle": self.obstacle,
            "obstacleDistancePct": self.obstacle_distance_pct,
            "obstacleBeforeFirstTake": self.obstacle_before_first_take,
            "ownBreakoutLevelExempted": self.own_breakout_level_exempted,
            "riskScale": self.risk_scale,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class SemanticCandidateAssessment:
    strategy: str
    setup_id: str | None
    action: str
    allowed: bool
    blockers: tuple[str, ...]
    confluence_strategies: tuple[str, ...]
    conflicting_strategies: tuple[str, ...]
    structural_path: StructuralPathAssessment
    flow_classification: str
    liquidity_classification: str
    freshness_classification: str
    flow_priority: int
    liquidity_priority: int
    freshness_priority: int
    risk_scale: float
    reasons: tuple[str, ...]

    @property
    def confluence_count(self) -> int:
        return len(self.confluence_strategies)

    def public(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "setupId": self.setup_id,
            "action": self.action,
            "allowed": self.allowed,
            "blockers": list(self.blockers),
            "confluenceStrategies": list(self.confluence_strategies),
            "conflictingStrategies": list(self.conflicting_strategies),
            "confluenceCount": self.confluence_count,
            "structuralPath": self.structural_path.public(),
            "flowClassification": self.flow_classification,
            "liquidityClassification": self.liquidity_classification,
            "freshnessClassification": self.freshness_classification,
            "flowPriority": self.flow_priority,
            "liquidityPriority": self.liquidity_priority,
            "freshnessPriority": self.freshness_priority,
            "riskScale": self.risk_scale,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class SelectionPriority:
    confluence_count: int
    flow_priority: int
    liquidity_priority: int
    freshness_priority: int
    net_reward_risk: float
    entry_drift_quality: float
    market_attention: float
    activity_rank_quality: int
    deterministic_key: str

    def key(self) -> tuple:
        return (
            self.confluence_count,
            self.flow_priority,
            self.liquidity_priority,
            self.freshness_priority,
            self.net_reward_risk,
            self.entry_drift_quality,
            self.market_attention,
            self.activity_rank_quality,
            self.deterministic_key,
        )

    def public(self) -> dict[str, Any]:
        return {
            "confluenceCount": self.confluence_count,
            "flowPriority": self.flow_priority,
            "liquidityPriority": self.liquidity_priority,
            "freshnessPriority": self.freshness_priority,
            "netRewardRisk": self.net_reward_risk,
            "entryDriftQuality": self.entry_drift_quality,
            "marketAttention": self.market_attention,
            "activityRankQuality": self.activity_rank_quality,
            "deterministicKey": self.deterministic_key,
            "selectionOrder": [
                "confluence",
                "flow",
                "liquidity",
                "freshness",
                "net_reward_risk",
                "entry_drift",
                "market_attention",
                "activity_rank",
                "deterministic_key",
            ],
        }


def _details_mapping(
    decision: StrategyDecision,
    key: str,
) -> dict[str, Any]:
    value = (decision.details or {}).get(key)
    return value if isinstance(value, dict) else {}


def _mature_obstacle(level: StructuralLevel | None) -> bool:
    if level is None or level.lifecycle == "broken":
        return False
    if level.kind in {
        "day_high",
        "day_low",
        "previous_day_high",
        "previous_day_low",
    }:
        # Session and previous-session extremes are structural references by
        # definition; they do not need three local touches to matter.
        return True
    return (
        level.lifecycle == "worked"
        or level.distinct_approaches >= 3
        or level.touches >= 3
    )


def _zone_overlaps_level(
    decision: StrategyDecision,
    level: StructuralLevel,
) -> bool:
    zone = (decision.details or {}).get("zone")
    if not isinstance(zone, dict):
        return False
    low = zone.get("low")
    high = zone.get("high")
    if not isinstance(low, (int, float)) or not isinstance(
        high,
        (int, float),
    ):
        return False
    return (
        float(high) >= level.low
        and float(low) <= level.high
    )


def _decision_owns_level(
    decision: StrategyDecision,
    level: StructuralLevel,
) -> bool:
    # Daily/session extremes are independent structural market objects.
    # They can overlap the traded detector zone, but must never inherit its
    # own-level exemption even if malformed/replayed telemetry accidentally
    # reuses a generation id.
    if level.kind not in {"support", "resistance"}:
        return False

    details = decision.details or {}
    lifecycle = details.get("levelLifecycle")
    if isinstance(lifecycle, dict):
        decision_generation = lifecycle.get("generation_id")
        if (
            decision_generation
            and level.generation_id
        ):
            return (
                str(decision_generation)
                == str(level.generation_id)
            )

    # Fallback only for ordinary detector zones that predate exact generation
    # telemetry.
    return _zone_overlaps_level(decision, level)


def assess_structural_path(
    decision: StrategyDecision,
    context: MarketContext | None,
    *,
    partial_take_at_r: float = 1.0,
    partial_take_enabled: bool = True,
) -> StructuralPathAssessment:
    planned_partial = (
        (decision.details or {}).get(
            "plannedPartialEnabled"
        )
    )
    if isinstance(planned_partial, bool):
        partial_take_enabled = planned_partial

    side = decision.action.value
    if (
        not decision.tradeable
        or decision.entry is None
        or decision.stop is None
        or context is None
        or context.structure is None
    ):
        return StructuralPathAssessment(
            blocked=False,
            side=side,
            first_take_price=None,
            first_take_distance_pct=None,
            obstacle=None,
            obstacle_distance_pct=None,
            obstacle_before_first_take=False,
            own_breakout_level_exempted=False,
            risk_scale=1.0,
            reasons=("structural path unavailable or decision not tradeable",),
        )

    entry = float(decision.entry)
    stop = float(decision.stop)
    target = float(decision.target) if decision.target is not None else entry
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0:
        return StructuralPathAssessment(
            blocked=False,
            side=side,
            first_take_price=None,
            first_take_distance_pct=None,
            obstacle=None,
            obstacle_distance_pct=None,
            obstacle_before_first_take=False,
            own_breakout_level_exempted=False,
            risk_scale=1.0,
            reasons=("invalid entry/stop geometry for structural path",),
        )

    first_take_distance = (
        risk * max(0.0, partial_take_at_r)
        if partial_take_enabled and partial_take_at_r > 0
        else abs(target - entry)
    )
    if decision.action == Action.LONG:
        first_take = entry + first_take_distance
        directional_rows = (
            list(context.structure.resistance_levels)
            if context.structure.resistance_levels
            else (
                [context.structure.nearest_resistance]
                if context.structure.nearest_resistance is not None
                else []
            )
        )
        mature_rows = [
            level
            for level in directional_rows
            if (
                level.high >= entry
                and _mature_obstacle(level)
            )
        ]
        obstacle = (
            min(
                mature_rows,
                key=lambda level: (
                    max(0.0, level.low - entry),
                    abs(level.center - entry),
                    -level.score,
                ),
            )
            if mature_rows
            else None
        )
    else:
        first_take = entry - first_take_distance
        directional_rows = (
            list(context.structure.support_levels)
            if context.structure.support_levels
            else (
                [context.structure.nearest_support]
                if context.structure.nearest_support is not None
                else []
            )
        )
        mature_rows = [
            level
            for level in directional_rows
            if (
                level.low <= entry
                and _mature_obstacle(level)
            )
        ]
        obstacle = (
            min(
                mature_rows,
                key=lambda level: (
                    max(0.0, entry - level.high),
                    abs(level.center - entry),
                    -level.score,
                ),
            )
            if mature_rows
            else None
        )

    if obstacle is None:
        return StructuralPathAssessment(
            blocked=False,
            side=side,
            first_take_price=first_take,
            first_take_distance_pct=(
                first_take_distance / entry
                if entry > 0
                else None
            ),
            obstacle=(
                obstacle.public()
                if obstacle is not None
                else None
            ),
            obstacle_distance_pct=None,
            obstacle_before_first_take=False,
            own_breakout_level_exempted=False,
            risk_scale=1.0,
            reasons=("no mature opposing structural obstacle before entry path",),
        )

    if decision.action == Action.LONG:
        intersects_path = (
            obstacle.high >= entry
            and obstacle.low <= first_take
        )
        obstacle_distance = max(
            0.0,
            obstacle.low - entry,
        ) / entry
    else:
        intersects_path = (
            obstacle.low <= entry
            and obstacle.high >= first_take
        )
        obstacle_distance = max(
            0.0,
            entry - obstacle.high,
        ) / entry

    own_breakout = (
        decision.strategy == "level_breakout"
        and str((decision.details or {}).get("state") or "")
        in {"break", "impulse"}
        and _decision_owns_level(decision, obstacle)
    )

    managed_breakout_obstacle = (
        intersects_path
        and not own_breakout
        and decision.strategy == "level_breakout"
    )
    blocked = (
        intersects_path
        and not own_breakout
        and not managed_breakout_obstacle
    )
    risk_scale = 0.65 if managed_breakout_obstacle else 1.0
    reasons: list[str] = []
    if own_breakout:
        reasons.append(
            "nearest mature obstacle is the breakout's own accepted level"
        )
    elif managed_breakout_obstacle:
        reasons.append(
            "next mature obstacle is managed for breakout by reduced risk instead of binary veto"
        )
    elif blocked:
        reasons.append(
            "mature opposing structural level intersects path to first take"
        )
    else:
        reasons.append(
            "mature opposing structural level lies outside path to first take"
        )

    return StructuralPathAssessment(
        blocked=blocked,
        side=side,
        first_take_price=first_take,
        first_take_distance_pct=(
            first_take_distance / entry
            if entry > 0
            else None
        ),
        obstacle=obstacle.public(),
        obstacle_distance_pct=obstacle_distance,
        obstacle_before_first_take=intersects_path,
        own_breakout_level_exempted=own_breakout,
        risk_scale=risk_scale,
        reasons=tuple(reasons),
    )


def _same_location(
    left: StrategyDecision,
    right: StrategyDecision,
    *,
    max_distance_pct: float = 0.006,
) -> bool:
    left_level = left.watched_level
    right_level = right.watched_level
    if not isinstance(left_level, (int, float)) or not isinstance(
        right_level,
        (int, float),
    ):
        return True
    base = max(
        abs(float(left_level)),
        abs(float(right_level)),
        1e-9,
    )
    return (
        abs(float(left_level) - float(right_level)) / base
        <= max_distance_pct
    )


def _raw_assessment(
    decision: StrategyDecision,
    context: MarketContext | None,
    *,
    partial_take_at_r: float,
    partial_take_enabled: bool,
) -> SemanticCandidateAssessment:
    blockers: list[str] = []
    reasons: list[str] = []

    structural_path = assess_structural_path(
        decision,
        context,
        partial_take_at_r=partial_take_at_r,
        partial_take_enabled=partial_take_enabled,
    )
    if structural_path.blocked:
        blockers.append("mature_structural_obstacle_before_first_take")

    entry_context = _details_mapping(
        decision,
        "entryContextAssessment",
    )
    if entry_context.get("allowed") is False:
        blockers.extend(
            str(value)
            for value in (
                entry_context.get("blockers") or []
            )
        )

    if context is not None and not context.execution.ready:
        blockers.append("execution_context_not_ready")

    flow = _details_mapping(decision, "flowAlignment")
    liquidity = _details_mapping(
        decision,
        "liquidityAlignment",
    )
    freshness = _details_mapping(
        decision,
        "opportunityFreshness",
    )
    if not freshness:
        freshness = _details_mapping(
            decision,
            "entryFreshness",
        )
    flow_class = str(
        flow.get("classification") or "insufficient_data"
    )
    liquidity_class = str(
        liquidity.get("classification") or "unknown"
    )
    freshness_class = str(
        freshness.get("classification") or "unknown"
    )

    if freshness_class == "exhausted":
        blockers.append("opportunity_exhausted")

    managed_breakout_obstacle = (
        decision.strategy == "level_breakout"
        and structural_path.obstacle_before_first_take
        and not structural_path.own_breakout_level_exempted
        and structural_path.risk_scale < 1.0
    )
    if managed_breakout_obstacle:
        obstacle_consumption_ready = (
            freshness_class in {"fresh", "acceptable"}
            and flow_class in {"strongly_aligned", "aligned"}
            and liquidity_class != "opposed"
        )
        if not obstacle_consumption_ready:
            blockers.append(
                "managed_structural_obstacle_requires_breakout_strength"
            )
        else:
            reasons.append(
                "breakout may challenge the next mature level only with "
                "fresh/aligned evidence and reduced structural risk"
            )

    risk_scale = structural_path.risk_scale
    if freshness_class == "late":
        risk_scale = min(risk_scale, 0.65)
    elif freshness_class == "acceptable":
        risk_scale = min(risk_scale, 0.85)
    elif freshness_class == "unknown":
        risk_scale = min(risk_scale, 0.80)
    elif (
        freshness_class == "fresh"
        and structural_path.risk_scale >= 1.0
        and flow_class in {"strongly_aligned", "aligned"}
        and liquidity_class != "opposed"
    ):
        reasons.append(
            "fresh aligned evidence is recorded, but positive risk scaling "
            "is disabled until the playbook proves positive expectancy"
        )

    # Stage 19B: evidence may reduce risk, never increase it. The previous
    # +20%/+10% sizing amplified false breakouts before edge was established.
    risk_scale = max(0.0, min(risk_scale, 1.0))

    reasons.extend(structural_path.reasons)
    if flow_class:
        reasons.append(f"flow={flow_class}")
    if liquidity_class:
        reasons.append(f"liquidity={liquidity_class}")
    if freshness_class:
        reasons.append(f"freshness={freshness_class}")

    return SemanticCandidateAssessment(
        strategy=decision.strategy,
        setup_id=decision.setup_id,
        action=decision.action.value,
        allowed=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        confluence_strategies=(),
        conflicting_strategies=(),
        structural_path=structural_path,
        flow_classification=flow_class,
        liquidity_classification=liquidity_class,
        freshness_classification=freshness_class,
        flow_priority=FLOW_PRIORITY.get(flow_class, 0),
        liquidity_priority=LIQUIDITY_PRIORITY.get(
            liquidity_class,
            0,
        ),
        freshness_priority=FRESHNESS_PRIORITY.get(
            freshness_class,
            2,
        ),
        risk_scale=risk_scale,
        reasons=tuple(reasons),
    )


def assess_candidate(
    decision: StrategyDecision,
    context: MarketContext | None,
    *,
    partial_take_at_r: float = 1.0,
    partial_take_enabled: bool = True,
) -> SemanticCandidateAssessment:
    return _raw_assessment(
        decision,
        context,
        partial_take_at_r=partial_take_at_r,
        partial_take_enabled=partial_take_enabled,
    )


def assess_session_candidates(
    decisions: Iterable[StrategyDecision],
    context: MarketContext | None,
    *,
    partial_take_at_r: float = 1.0,
    partial_take_enabled: bool = True,
) -> dict[str, SemanticCandidateAssessment]:
    rows = [
        decision
        for decision in decisions
        if (
            decision.tradeable
            and decision.strategy in PLAYBOOK_KEYS
        )
    ]
    raw = {
        decision.strategy: _raw_assessment(
            decision,
            context,
            partial_take_at_r=partial_take_at_r,
            partial_take_enabled=partial_take_enabled,
        )
        for decision in rows
    }
    viable = {
        strategy
        for strategy, assessment in raw.items()
        if assessment.allowed
    }

    result: dict[str, SemanticCandidateAssessment] = {}
    for decision in rows:
        assessment = raw[decision.strategy]
        if decision.strategy not in viable:
            result[decision.strategy] = assessment
            continue

        same_side: list[str] = []
        opposite: list[str] = []
        for peer in rows:
            if (
                peer.strategy == decision.strategy
                or peer.strategy not in viable
            ):
                continue
            if not _same_location(decision, peer):
                continue
            if peer.action == decision.action:
                same_side.append(peer.strategy)
            elif (
                peer.action in {Action.LONG, Action.SHORT}
                and decision.action in {Action.LONG, Action.SHORT}
            ):
                opposite.append(peer.strategy)

        blockers = list(assessment.blockers)
        reasons = list(assessment.reasons)
        if opposite:
            # Keep the conflict explicit for selection/telemetry, but do not
            # veto both candidates symmetrically. The global arbiter already
            # compares flow, liquidity, freshness, payoff and drift and must
            # be allowed to choose one side.
            reasons.append(
                "opposite tradeable playbook exists at the same market "
                "location; defer side choice to global selection priority"
            )
        if same_side:
            reasons.append(
                "same-direction playbook confluence: "
                + ", ".join(sorted(same_side))
            )

        result[decision.strategy] = replace(
            assessment,
            allowed=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
            confluence_strategies=tuple(sorted(same_side)),
            conflicting_strategies=tuple(sorted(opposite)),
            reasons=tuple(reasons),
        )

    return result


def build_selection_priority(
    assessment: SemanticCandidateAssessment,
    plan: TradePlan,
    *,
    activity_rank: int,
    activity_score: float,
) -> SelectionPriority:
    return SelectionPriority(
        confluence_count=assessment.confluence_count,
        flow_priority=assessment.flow_priority,
        liquidity_priority=assessment.liquidity_priority,
        freshness_priority=assessment.freshness_priority,
        net_reward_risk=max(0.0, float(plan.net_reward_risk)),
        entry_drift_quality=-abs(float(plan.entry_drift_pct)),
        market_attention=max(
            0.0,
            min(float(activity_score), 100.0),
        ),
        activity_rank_quality=-max(
            1,
            min(int(activity_rank), 999),
        ),
        deterministic_key=(
            f"{plan.symbol}:{plan.strategy}:{plan.side.value}:"
            f"{plan.setup_id}"
        ),
    )
