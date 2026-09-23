from scalp_bot.domain import Action, StrategyDecision, Trend
from scalp_bot.strategy.liquidity_evidence import (
    LiquidityAlignmentClass,
    LiquidityEvidenceState,
    build_liquidity_evidence,
)


def density_decision(
    *,
    state: str,
    wall_side: str = "ask",
    wall_price: float = 100.0,
    reason: str | None = None,
    remaining: float = 1.0,
    attack: float = 0.0,
    depletion: float = 0.0,
    replenishment: float = 0.0,
    absorption: bool = False,
    wall_present: bool = True,
    lost_significance: bool = False,
    consuming: bool = False,
    causes: list[str] | None = None,
    action: Action = Action.WAIT,
) -> StrategyDecision:
    details = {
        "state": state,
        "wallSide": wall_side,
        "wallPrice": wall_price,
        "remainingRatio": remaining,
        "attackRatio": attack,
        "depletionPerSecond": depletion,
        "replenishmentRatio": replenishment,
        "absorptionObserved": absorption,
        "wallPresent": wall_present,
        "lostSignificance": lost_significance,
        "consuming": consuming,
        "strengthMultiple": 8.0,
        "distancePct": 0.001,
    }
    if reason is not None:
        details["reason"] = reason
    if causes is not None:
        details["consumptionCause"] = causes
    return StrategyDecision(
        strategy="orderbook_density",
        action=action,
        reasons=["density"],
        watched_level=wall_price,
        entry=100.0 if action != Action.WAIT else None,
        stop=99.5 if action == Action.LONG else (100.5 if action == Action.SHORT else None),
        target=101.0 if action == Action.LONG else (99.0 if action == Action.SHORT else None),
        details=details,
    )


def test_consumed_ask_is_bullish_evidence_not_removed_wall() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="exhausted",
            remaining=0.60,
            attack=0.45,
            depletion=0.12,
            consuming=True,
            causes=["remaining_ratio", "aggressive_attack"],
        )
    )

    assert evidence.state == LiquidityEvidenceState.CONSUMED
    assert evidence.directional_bias == Trend.UP
    assert evidence.directional_strength >= 0.8

    long_alignment = evidence.alignment_for(Action.LONG)
    short_alignment = evidence.alignment_for(Action.SHORT)
    assert long_alignment is not None
    assert short_alignment is not None
    assert long_alignment.classification == LiquidityAlignmentClass.SUPPORTIVE
    assert short_alignment.classification == LiquidityAlignmentClass.OPPOSED


def test_removed_wall_is_neutral_not_treated_as_consumption() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="reaction",
            reason="wall_removed_after_defense",
            wall_present=False,
            remaining=0.0,
        )
    )

    assert evidence.state == LiquidityEvidenceState.REMOVED
    assert evidence.directional_bias == Trend.FLAT
    alignment = evidence.alignment_for(Action.LONG)
    assert alignment is not None
    assert alignment.classification == LiquidityAlignmentClass.NEUTRAL


def test_replenishing_bid_supports_long() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="defended",
            wall_side="bid",
            remaining=0.95,
            replenishment=0.18,
            absorption=True,
        )
    )

    assert evidence.state == LiquidityEvidenceState.REPLENISHING
    assert evidence.directional_bias == Trend.UP
    alignment = evidence.alignment_for(Action.LONG)
    assert alignment is not None
    assert alignment.classification == LiquidityAlignmentClass.SUPPORTIVE


def test_defended_ask_opposes_long_and_supports_short() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="defended",
            wall_side="ask",
            absorption=False,
        )
    )

    assert evidence.state == LiquidityEvidenceState.DEFENDED
    assert evidence.directional_bias == Trend.DOWN
    long_alignment = evidence.alignment_for(Action.LONG)
    short_alignment = evidence.alignment_for(Action.SHORT)
    assert long_alignment is not None
    assert short_alignment is not None
    assert long_alignment.classification == LiquidityAlignmentClass.OPPOSED
    assert short_alignment.classification == LiquidityAlignmentClass.SUPPORTIVE


def test_lost_significance_is_neutral_context() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="exhausted",
            reason="wall_lost_significance",
            lost_significance=True,
        )
    )

    assert evidence.state == LiquidityEvidenceState.LOST_SIGNIFICANCE
    assert evidence.directional_bias == Trend.FLAT


def test_exhausted_cooldown_is_unknown_not_tracking() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="exhausted",
            reason="wall_exhausted_cooldown",
        )
    )

    assert evidence.state == LiquidityEvidenceState.UNKNOWN


def test_shadow_action_is_preserved_from_raw_density_signal() -> None:
    evidence = build_liquidity_evidence(
        density_decision(
            state="reaction",
            action=Action.SHORT,
            absorption=True,
        )
    )

    assert evidence.shadow_action == "short"
