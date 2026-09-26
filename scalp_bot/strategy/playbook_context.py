from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..domain import Action, Side, Trend
from .flow_context import FlowAlignmentClass
from .market_context import MarketContext
from .regime import HTFBias, LocalRegime


class PlaybookKind(StrEnum):
    TREND_CONTINUATION = "trend_continuation"
    LEVEL_BREAKOUT = "level_breakout"
    LEVEL_REJECTION = "level_rejection"


@dataclass(frozen=True, slots=True)
class DirectionPlan:
    playbook: PlaybookKind
    allowed_directions: tuple[Trend, ...]
    primary_direction: Trend
    source: str
    local_regime: str | None
    htf_bias: str | None
    reasons: tuple[str, ...]

    @property
    def directional(self) -> bool:
        return self.primary_direction in {Trend.UP, Trend.DOWN}

    def public(self) -> dict[str, Any]:
        return {
            "playbook": self.playbook.value,
            "allowedDirections": [
                value.value for value in self.allowed_directions
            ],
            "primaryDirection": self.primary_direction.value,
            "source": self.source,
            "localRegime": self.local_regime,
            "htfBias": self.htf_bias,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class EntryContextAssessment:
    playbook: PlaybookKind
    action: Action
    allowed: bool
    direction_plan: DirectionPlan
    flow_classification: str | None
    liquidity_classification: str | None
    blockers: tuple[str, ...]
    reasons: tuple[str, ...]
    htf_override: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "playbook": self.playbook.value,
            "action": self.action.value,
            "allowed": self.allowed,
            "directionPlan": self.direction_plan.public(),
            "flowClassification": self.flow_classification,
            "liquidityClassification": self.liquidity_classification,
            "htfOverride": self.htf_override,
            "blockers": list(self.blockers),
            "reasons": list(self.reasons),
        }


def _fallback_plan(
    playbook: PlaybookKind,
    fallback_trend: Trend,
) -> DirectionPlan:
    directions = (
        (fallback_trend,)
        if fallback_trend in {Trend.UP, Trend.DOWN}
        else ()
    )
    return DirectionPlan(
        playbook=playbook,
        allowed_directions=directions,
        primary_direction=(
            fallback_trend
            if directions
            else Trend.FLAT
        ),
        source="legacy_fallback",
        local_regime=None,
        htf_bias=None,
        reasons=(
            "MarketContext unavailable; legacy trend fallback is used",
        ),
    )


def _primary_direction(
    directions: tuple[Trend, ...],
    source: str,
) -> Trend:
    if not directions:
        return Trend.FLAT
    if source in {
        "range_two_sided",
        "pullback_two_sided",
        "transition_two_sided",
        "unclear_two_sided",
    }:
        return Trend.FLAT
    return directions[0]


def _context_meta(
    context: MarketContext,
) -> tuple[str | None, str | None]:
    local = (
        context.local_regime.regime.value
        if context.local_regime is not None
        else None
    )
    htf = (
        context.htf_bias.bias.value
        if context.htf_bias is not None
        else None
    )
    return local, htf


def continuation_direction_plan(
    context: MarketContext | None,
    fallback_trend: Trend,
) -> DirectionPlan:
    playbook = PlaybookKind.TREND_CONTINUATION
    if context is None or context.local_regime is None:
        return _fallback_plan(playbook, fallback_trend)

    local = context.local_regime
    regime = local.regime
    direction = Trend.FLAT
    reasons: list[str] = []

    if regime in {
        LocalRegime.BULLISH_TREND,
        LocalRegime.BULLISH_IMPULSE,
    }:
        direction = Trend.UP
        reasons.append(
            "local regime is bullish and supplies continuation direction"
        )
    elif regime in {
        LocalRegime.BEARISH_TREND,
        LocalRegime.BEARISH_IMPULSE,
    }:
        direction = Trend.DOWN
        reasons.append(
            "local regime is bearish and supplies continuation direction"
        )
    elif regime == LocalRegime.PULLBACK:
        if local.parent_direction in {Trend.UP, Trend.DOWN}:
            direction = local.parent_direction
            reasons.append(
                "local pullback inherits the 5m parent direction"
            )
        else:
            reasons.append(
                "pullback has no directional 5m parent"
            )
    else:
        reasons.append(
            f"{regime.value} is not a continuation regime"
        )

    if context.htf_bias is not None:
        expected_bias = (
            HTFBias.BULLISH
            if direction == Trend.UP
            else (
                HTFBias.BEARISH
                if direction == Trend.DOWN
                else HTFBias.NEUTRAL
            )
        )
        if (
            direction in {Trend.UP, Trend.DOWN}
            and context.htf_bias.bias not in {
                expected_bias,
                HTFBias.NEUTRAL,
            }
        ):
            reasons.append(
                "HTF bias disagrees with local direction but is context-only"
            )

    local_name, htf_name = _context_meta(context)
    directions = (direction,) if direction != Trend.FLAT else ()
    return DirectionPlan(
        playbook=playbook,
        allowed_directions=directions,
        primary_direction=direction,
        source="local_regime",
        local_regime=local_name,
        htf_bias=htf_name,
        reasons=tuple(reasons),
    )


def breakout_direction_plan(
    context: MarketContext | None,
    fallback_trend: Trend,
) -> DirectionPlan:
    playbook = PlaybookKind.LEVEL_BREAKOUT
    if context is None or context.local_regime is None:
        return _fallback_plan(playbook, fallback_trend)

    local = context.local_regime
    regime = local.regime
    directions: tuple[Trend, ...]
    source = "local_regime"
    reasons: list[str] = []

    if regime in {
        LocalRegime.BULLISH_TREND,
        LocalRegime.BULLISH_IMPULSE,
    }:
        directions = (Trend.UP, Trend.DOWN)
        source = "local_regime_preference_two_sided"
        reasons.append(
            "bullish local regime is preference/context only; both breakout "
            "directions require their own level/flow evidence"
        )
    elif regime in {
        LocalRegime.BEARISH_TREND,
        LocalRegime.BEARISH_IMPULSE,
    }:
        directions = (Trend.DOWN, Trend.UP)
        source = "local_regime_preference_two_sided"
        reasons.append(
            "bearish local regime is preference/context only; both breakout "
            "directions require their own level/flow evidence"
        )
    elif regime == LocalRegime.PULLBACK:
        if local.parent_direction in {Trend.UP, Trend.DOWN}:
            opposite = (
                Trend.DOWN
                if local.parent_direction == Trend.UP
                else Trend.UP
            )
            directions = (
                local.parent_direction,
                opposite,
            )
            source = "parent_preference_two_sided"
            reasons.append(
                "pullback parent direction is preference only; breakout "
                "direction must be proven by live level/flow evidence"
            )
        else:
            directions = (Trend.UP, Trend.DOWN)
            source = "pullback_two_sided"
            reasons.append(
                "pullback without parent permits either evidence-confirmed breakout"
            )
    elif regime == LocalRegime.RANGE:
        directions = (Trend.UP, Trend.DOWN)
        source = "range_two_sided"
        reasons.append(
            "range regime permits either accepted boundary breakout"
        )
    elif regime == LocalRegime.TRANSITION:
        if local.direction in {Trend.UP, Trend.DOWN}:
            opposite = (
                Trend.DOWN
                if local.direction == Trend.UP
                else Trend.UP
            )
            directions = (local.direction, opposite)
            source = "transition_preference_two_sided"
            reasons.append(
                "transition direction is preference only; either breakout "
                "side still requires its own level/flow evidence"
            )
        else:
            directions = (Trend.UP, Trend.DOWN)
            source = "transition_two_sided"
            reasons.append(
                "directionless transition allows either confirmed breakout"
            )
    else:
        directions = (Trend.UP, Trend.DOWN)
        source = "unclear_two_sided"
        reasons.append(
            "unclear local regime is context-only; an evidence-confirmed "
            "breakout may establish its own direction"
        )

    local_name, htf_name = _context_meta(context)
    return DirectionPlan(
        playbook=playbook,
        allowed_directions=directions,
        primary_direction=_primary_direction(
            directions,
            source,
        ),
        source=source,
        local_regime=local_name,
        htf_bias=htf_name,
        reasons=tuple(reasons),
    )


def rejection_direction_plan(
    context: MarketContext | None,
    fallback_trend: Trend,
) -> DirectionPlan:
    playbook = PlaybookKind.LEVEL_REJECTION
    if context is None or context.local_regime is None:
        return _fallback_plan(playbook, fallback_trend)

    local = context.local_regime
    regime = local.regime
    directions: tuple[Trend, ...]
    source = "local_regime"
    reasons: list[str] = []

    if regime in {
        LocalRegime.BULLISH_TREND,
        LocalRegime.BULLISH_IMPULSE,
    }:
        directions = (Trend.UP, Trend.DOWN)
        source = "local_regime_preference_two_sided"
        reasons.append(
            "bullish local regime is context, not a side veto; rejection "
            "direction is decided by the tested level and local response"
        )
    elif regime in {
        LocalRegime.BEARISH_TREND,
        LocalRegime.BEARISH_IMPULSE,
    }:
        directions = (Trend.DOWN, Trend.UP)
        source = "local_regime_preference_two_sided"
        reasons.append(
            "bearish local regime is context, not a side veto; rejection "
            "direction is decided by the tested level and local response"
        )
    elif regime == LocalRegime.PULLBACK:
        if local.parent_direction in {Trend.UP, Trend.DOWN}:
            opposite = (
                Trend.DOWN
                if local.parent_direction == Trend.UP
                else Trend.UP
            )
            directions = (
                local.parent_direction,
                opposite,
            )
            source = "parent_preference_two_sided"
            reasons.append(
                "pullback parent is a preference only; level rejection must "
                "prove its direction with failed-break/absorption evidence"
            )
        else:
            directions = (Trend.UP, Trend.DOWN)
            source = "pullback_two_sided"
            reasons.append(
                "pullback without parent permits either evidence-confirmed rejection"
            )
    elif regime == LocalRegime.RANGE:
        directions = (Trend.UP, Trend.DOWN)
        source = "range_two_sided"
        reasons.append(
            "range permits support-long and resistance-short rejection"
        )
    elif regime == LocalRegime.TRANSITION:
        if local.direction in {Trend.UP, Trend.DOWN}:
            opposite = (
                Trend.DOWN
                if local.direction == Trend.UP
                else Trend.UP
            )
            directions = (local.direction, opposite)
            source = "transition_preference_two_sided"
            reasons.append(
                "transition direction is preference only; a failed-break "
                "rejection may establish either side with local evidence"
            )
        else:
            directions = (Trend.UP, Trend.DOWN)
            source = "transition_two_sided"
            reasons.append(
                "directionless transition allows either evidence-confirmed rejection"
            )
    else:
        directions = (Trend.UP, Trend.DOWN)
        source = "unclear_two_sided"
        reasons.append(
            "unclear local regime is context-only; failed-break/absorption "
            "evidence may establish rejection direction"
        )

    local_name, htf_name = _context_meta(context)
    return DirectionPlan(
        playbook=playbook,
        allowed_directions=directions,
        primary_direction=_primary_direction(
            directions,
            source,
        ),
        source=source,
        local_regime=local_name,
        htf_bias=htf_name,
        reasons=tuple(reasons),
    )


def direction_for_action(action: Action) -> Trend:
    if action == Action.LONG:
        return Trend.UP
    if action == Action.SHORT:
        return Trend.DOWN
    return Trend.FLAT


def action_for_direction(direction: Trend) -> Action:
    if direction == Trend.UP:
        return Action.LONG
    if direction == Trend.DOWN:
        return Action.SHORT
    return Action.WAIT


def assess_entry_context(
    playbook: PlaybookKind,
    action: Action,
    context: MarketContext | None,
    fallback_trend: Trend,
    *,
    breakout_confirmed: bool = False,
) -> EntryContextAssessment:
    if playbook == PlaybookKind.TREND_CONTINUATION:
        plan = continuation_direction_plan(context, fallback_trend)
    elif playbook == PlaybookKind.LEVEL_BREAKOUT:
        plan = breakout_direction_plan(context, fallback_trend)
    else:
        plan = rejection_direction_plan(context, fallback_trend)

    blockers: list[str] = []
    reasons = list(plan.reasons)
    direction = direction_for_action(action)
    if direction not in plan.allowed_directions:
        blockers.append("action_not_allowed_by_local_regime")

    flow_classification = None
    liquidity_classification = None
    htf_override = None
    if context is not None:
        flow_alignment = context.flow_alignment_for(action)
        if flow_alignment is not None:
            flow_classification = flow_alignment.classification.value
            if playbook == PlaybookKind.TREND_CONTINUATION and (
                flow_alignment.classification
                in {
                    FlowAlignmentClass.OPPOSED,
                    FlowAlignmentClass.SHORT_TERM_REVERSAL,
                }
            ):
                blockers.append(
                    f"continuation_flow_{flow_alignment.classification.value}"
                )
            elif (
                playbook == PlaybookKind.LEVEL_BREAKOUT
                and flow_alignment.classification
                == FlowAlignmentClass.OPPOSED
            ):
                blockers.append("breakout_flow_opposed")
            elif (
                playbook == PlaybookKind.LEVEL_REJECTION
                and flow_alignment.classification
                == FlowAlignmentClass.OPPOSED
            ):
                blockers.append("rejection_flow_opposed")

        liquidity_alignment = context.liquidity_alignment_for(action)
        if liquidity_alignment is not None:
            liquidity_classification = (
                liquidity_alignment.classification.value
            )

        if (
            playbook == PlaybookKind.LEVEL_BREAKOUT
            and context.htf_bias is not None
        ):
            htf_bias = context.htf_bias.bias
            counter_htf = (
                action == Action.LONG
                and htf_bias == HTFBias.BEARISH
            ) or (
                action == Action.SHORT
                and htf_bias == HTFBias.BULLISH
            )
            if counter_htf:
                htf = context.htf_bias
                local = context.local_regime
                matching_regimes = (
                    {LocalRegime.BULLISH_TREND, LocalRegime.BULLISH_IMPULSE}
                    if action == Action.LONG
                    else {LocalRegime.BEARISH_TREND, LocalRegime.BEARISH_IMPULSE}
                )
                # An isolated hourly bias is context, not an absolute veto,
                # once the strategy has confirmed the break and local evidence
                # agrees. Raw/probe entries must never use this exception.
                if (
                    breakout_confirmed
                    and htf.alignment == "1h_only"
                    and htf.trend_15m == Trend.FLAT
                    and htf.trend_1h == (
                        Trend.DOWN if action == Action.LONG else Trend.UP
                    )
                    and local is not None
                    and local.regime in matching_regimes
                    and local.direction == direction
                    and flow_alignment is not None
                    and flow_alignment.classification
                    == FlowAlignmentClass.STRONGLY_ALIGNED
                ):
                    htf_override = "confirmed_local_breakout"
                    reasons.append(
                        "1h_only opposition is context-only: confirmed breakout, "
                        "aligned local regime and 5s/15s/60s flow"
                    )
                else:
                    blockers.append("breakout_htf_opposed")

        if (
            playbook == PlaybookKind.LEVEL_REJECTION
            and flow_alignment is not None
            and flow_alignment.classification
            == FlowAlignmentClass.SHORT_TERM_REVERSAL
            and context.local_regime is not None
        ):
            local_direction = context.local_regime.direction
            local_opposed = (
                action == Action.LONG
                and local_direction == Trend.DOWN
            ) or (
                action == Action.SHORT
                and local_direction == Trend.UP
            )
            longer_flow_opposed = {
                15,
                60,
            }.issubset(
                set(flow_alignment.opposed_horizons)
            )
            if local_opposed and longer_flow_opposed:
                blockers.append(
                    "rejection_local_and_longer_flow_opposed"
                )

        if not context.execution.ready:
            blockers.append("execution_context_not_ready")

    if blockers:
        reasons.append(
            "entry context blocked: " + ", ".join(blockers)
        )

    return EntryContextAssessment(
        playbook=playbook,
        action=action,
        allowed=not blockers,
        direction_plan=plan,
        flow_classification=flow_classification,
        liquidity_classification=liquidity_classification,
        blockers=tuple(blockers),
        reasons=tuple(reasons),
        htf_override=htf_override,
    )


def position_context_supported(
    playbook: PlaybookKind,
    side: Side,
    context: MarketContext | None,
    fallback_trend: Trend,
) -> bool:
    action = (
        Action.LONG
        if side == Side.LONG
        else Action.SHORT
    )
    if playbook == PlaybookKind.TREND_CONTINUATION:
        plan = continuation_direction_plan(context, fallback_trend)
    elif playbook == PlaybookKind.LEVEL_BREAKOUT:
        plan = breakout_direction_plan(context, fallback_trend)
    else:
        plan = rejection_direction_plan(context, fallback_trend)
    return direction_for_action(action) in plan.allowed_directions
