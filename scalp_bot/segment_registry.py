"""Shadow-only lifecycle over PR60's evidence store and eight-dimensional keys."""
from dataclasses import dataclass, asdict
from enum import StrEnum
import hashlib
import json

from .setup_segments import segment_key


class SegmentState(StrEnum):
    OBSERVING = "OBSERVING"
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    DORMANT = "DORMANT"


@dataclass(frozen=True)
class RegistryPolicy:
    minimum_samples: int = 100
    minimum_recent: int = 30
    minimum_captures: int = 3
    minimum_residence_ms: int = 3_600_000
    promote_lower_r: float = 0.05
    demote_upper_r: float = -0.05
    degrade_drawdown_r: float = 5.0
    dormant_drawdown_r: float = 10.0

    def __post_init__(self):
        if (self.minimum_samples < 2 or not 2 <= self.minimum_recent <= 200
                or self.minimum_captures < 2 or self.minimum_residence_ms < 0
                or not self.demote_upper_r < 0 < self.promote_lower_r
                or not 0 < self.degrade_drawdown_r < self.dormant_drawdown_r):
            raise ValueError("invalid preregistered registry policy")

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class SegmentRegistry:
    SCALES = dict(OBSERVING=0., TRIAL=.25, ACTIVE=1., DEGRADED=.5, DORMANT=0.)

    def __init__(self, evidence_book, policy=None):
        self.book = evidence_book
        self.policy = policy or RegistryPolicy()
        self.states = {}
        self.last_ms = None

    def assess(self, segment, *, now_ms, population="shadow"):
        if self.last_ms is not None and now_ms < self.last_ms:
            raise ValueError("registry wall time regressed; replay must be chronological")
        self.last_ms = now_ms
        key = (population, segment_key(segment))
        state, entered = self.states.setdefault(key, (SegmentState.OBSERVING, now_ms))
        evidence = self.book.lifecycle_evidence(segment, population=population)
        long, recent, p = evidence["longTerm"], evidence["recent"], self.policy
        ready = ("unknown" not in key[1] and long["samples"] >= p.minimum_samples
                 and recent["samples"] >= p.minimum_recent and evidence["captures"] >= p.minimum_captures)
        next_state, reason = state, "insufficient_independent_evidence"
        if ready:
            positive = long["lowerR"] > p.promote_lower_r and recent["lowerR"] > p.promote_lower_r
            negative = long["upperR"] < p.demote_upper_r and recent["upperR"] < p.demote_upper_r
            dd = evidence["drawdownR"]
            reason = "hysteresis_hold"
            if now_ms-entered < p.minimum_residence_ms:
                reason = "minimum_residence"
            elif state in (SegmentState.OBSERVING, SegmentState.DORMANT):
                if positive and dd < p.degrade_drawdown_r:
                    next_state, reason = SegmentState.TRIAL, "positive_bounds"
            elif negative or dd >= p.dormant_drawdown_r:
                next_state, reason = SegmentState.DORMANT, "negative_bounds_or_drawdown"
            elif recent["upperR"] < p.demote_upper_r or dd >= p.degrade_drawdown_r:
                next_state, reason = SegmentState.DEGRADED, "recent_weakness_or_drawdown"
            elif positive and dd < p.degrade_drawdown_r:
                next_state, reason = SegmentState.ACTIVE, "positive_bounds"
        if next_state != state:
            self.states[key] = (next_state, now_ms)
        return dict(mode="shadow", wouldState=next_state.value,
            wouldRiskScale=self.SCALES[next_state.value], reason=reason,
            previousState=state.value, transitioned=next_state != state,
            evidence=evidence, population=population, policyHash=p.digest,
            appliedRiskScale=1.0, redistributesRisk=False)
