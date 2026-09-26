"""Pure proposal-selection specification. No locks, orders or runtime wiring.

Readiness means a complete, fresh and provisionally costed proposal, NOT merely
an impulse probability. The future executor must atomically recheck ownership,
quote/economics and portfolio budget. This helper cannot authorize an order.
"""
from enum import StrEnum


class SymbolPhase(StrEnum):
    FREE = "free"
    OBSERVING = "observing"
    PREPARING = "preparing"
    READY = "ready"
    RESERVING = "reserving"
    ORDER_PENDING = "order_pending"
    PARTIAL_FILL = "partial_fill"
    IN_POSITION = "in_position"
    CANCELLING = "cancelling"
    RECONCILING = "reconciling"


class ProposalSource(StrEnum):
    NONE = "none"
    RULE = "rule"
    ML = "ml"


EXECUTION_OWNED = frozenset({
    SymbolPhase.RESERVING, SymbolPhase.ORDER_PENDING, SymbolPhase.PARTIAL_FILL,
    SymbolPhase.IN_POSITION, SymbolPhase.CANCELLING, SymbolPhase.RECONCILING,
})


def select_proposal(
    phase: SymbolPhase, *, rule_ready: bool, ml_ready: bool,
) -> ProposalSource:
    """Existing ready rule wins; qualified ML may replace observation only."""
    if not isinstance(phase, SymbolPhase) or type(rule_ready) is not bool or type(ml_ready) is not bool:
        raise ValueError("invalid arbitration input")
    if phase in EXECUTION_OWNED:
        return ProposalSource.NONE
    if rule_ready:
        return ProposalSource.RULE
    if phase == SymbolPhase.READY:
        return ProposalSource.NONE  # Conflicting state must not hand over ownership.
    return ProposalSource.ML if ml_ready else ProposalSource.NONE
