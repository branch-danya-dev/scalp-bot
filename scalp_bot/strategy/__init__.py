from .base import Strategy
from .breakout import BreakoutStage, LevelBreakoutStrategy
from .common import (
    LevelZone,
    classify_context_trend,
    classify_trend,
    compute_trade_flow,
    detect_level_zones,
)
from .density import DensityBounceStrategy, DensityStage
from .liquidity import LiquidityTarget, find_liquidity_target
from .lifecycle import LevelLifecycleTracker
from .freshness import (
    EntryFreshness,
    EntryFreshnessClass,
    OpportunityFreshness,
    OpportunityFreshnessClass,
    classify_entry_freshness,
    classify_opportunity_freshness,
)
from .flow_context import (
    FlowAlignment,
    FlowAlignmentClass,
    FlowHorizon,
    MultiHorizonFlowContext,
    build_multi_horizon_flow_context,
)
from .liquidity_evidence import (
    LiquidityAlignment,
    LiquidityAlignmentClass,
    LiquidityEvidence,
    LiquidityEvidenceState,
    build_liquidity_evidence,
)
from .market_context import (
    ExecutionContext,
    MarketContext,
    StructureContext,
    build_execution_context,
    build_structure_context,
)
from .pre_state import (
    FormingCandleContext,
    build_forming_candle_context,
)
from .playbook_context import (
    DirectionPlan,
    EntryContextAssessment,
    PlaybookKind,
    assess_entry_context,
    breakout_direction_plan,
    continuation_direction_plan,
    position_context_supported,
    rejection_direction_plan,
)
from .semantic_arbiter import (
    SelectionPriority,
    SemanticCandidateAssessment,
    StructuralPathAssessment,
    assess_candidate,
    assess_session_candidates,
    assess_structural_path,
    build_selection_priority,
)
from .regime import (
    HTFBias,
    HTFBiasSnapshot,
    LocalRegime,
    LocalRegimeSnapshot,
    classify_htf_bias,
    classify_local_regime,
)
from .structure import MarketStructure, StructuralLevel, TrendLine, build_market_structure
from .trend_structure import TrendStructureStrategy
from .weak_level_rejection import RejectionStage, WeakLevelRejectionStrategy
from .price_action_hypothesis import PriceActionHypothesisStrategy

def create_default_strategies() -> list[Strategy]:
    """Return fresh stateful strategy instances for one engine."""
    return [
        TrendStructureStrategy(),
        WeakLevelRejectionStrategy(),
        DensityBounceStrategy(),
        LevelBreakoutStrategy(),
        PriceActionHypothesisStrategy(),
    ]

__all__ = [
    "Strategy",
    "LevelZone",
    "classify_context_trend",
    "classify_trend",
    "compute_trade_flow",
    "detect_level_zones",
    "TrendStructureStrategy",
    "WeakLevelRejectionStrategy",
    "DensityBounceStrategy",
    "LevelBreakoutStrategy",
    "PriceActionHypothesisStrategy",
    "RejectionStage",
    "DensityStage",
    "LiquidityTarget",
    "find_liquidity_target",
    "LevelLifecycleTracker",
    "EntryFreshness",
    "EntryFreshnessClass",
    "OpportunityFreshness",
    "OpportunityFreshnessClass",
    "classify_entry_freshness",
    "classify_opportunity_freshness",
    "FlowAlignment",
    "FlowAlignmentClass",
    "FlowHorizon",
    "MultiHorizonFlowContext",
    "build_multi_horizon_flow_context",
    "LiquidityAlignment",
    "LiquidityAlignmentClass",
    "LiquidityEvidence",
    "LiquidityEvidenceState",
    "build_liquidity_evidence",
    "ExecutionContext",
    "MarketContext",
    "StructureContext",
    "build_execution_context",
    "build_structure_context",
    "FormingCandleContext",
    "build_forming_candle_context",
    "DirectionPlan",
    "EntryContextAssessment",
    "PlaybookKind",
    "assess_entry_context",
    "breakout_direction_plan",
    "continuation_direction_plan",
    "position_context_supported",
    "rejection_direction_plan",
    "SelectionPriority",
    "SemanticCandidateAssessment",
    "StructuralPathAssessment",
    "assess_candidate",
    "assess_session_candidates",
    "assess_structural_path",
    "build_selection_priority",
    "HTFBias",
    "HTFBiasSnapshot",
    "LocalRegime",
    "LocalRegimeSnapshot",
    "classify_htf_bias",
    "classify_local_regime",
    "MarketStructure",
    "StructuralLevel",
    "TrendLine",
    "build_market_structure",
    "BreakoutStage",
    "create_default_strategies",
]
