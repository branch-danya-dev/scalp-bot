from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .domain import Action, StrategyDecision, TradePlan
from .strategy.market_context import MarketContext
from .strategy.semantic_arbiter import SemanticCandidateAssessment


class PolicyMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class PolicyRuleType(StrEnum):
    BLOCK_FEATURE_VALUE = "block_feature_value"
    MIN_NET_REWARD_RISK = "min_net_reward_risk"


SUPPORTED_FEATURE_DIMENSIONS = {
    "flowAlignment",
    "entryFreshness",
    "liquidityAlignment",
    "confluenceCount",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_id(prefix: str, value: Any, length: int = 16) -> str:
    return f"{prefix}-{_sha256_bytes(_canonical_bytes(value))[:length]}"


def _policy_fingerprint_payload(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "policyId": manifest.get("policyId"),
        "version": manifest.get("version"),
        "allowEnforce": bool(
            manifest.get("allowEnforce")
        ),
        "source": manifest.get("source") or {},
        "rollback": manifest.get("rollback") or {},
        "rules": manifest.get("rules") or [],
    }


def _read_stability_source(
    source_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(source_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "stability-validation.json" in names:
                raw = archive.read("stability-validation.json")
                stability = json.loads(raw.decode("utf-8"))
            elif "cross-session-report.json" in names:
                raw_report = archive.read("cross-session-report.json")
                report = json.loads(raw_report.decode("utf-8"))
                stability = report.get("stabilityValidation") or {}
                raw = _canonical_bytes(stability)
            else:
                raise ValueError(
                    f"{path} does not contain stability validation"
                )
        return stability, {
            "sourcePath": str(path),
            "sourceKind": "research_dataset_zip",
            "sourceSha256": _sha256_bytes(path.read_bytes()),
            "stabilitySha256": _sha256_bytes(raw),
        }

    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(
            f"Expected JSON object in {path}"
        )
    if "stabilityValidation" in value:
        stability = value.get("stabilityValidation") or {}
        source_kind = "cross_session_report"
    elif (
        "featureEffects" in value
        or "fixedNetRewardRiskThresholds" in value
        or "thresholdSelectionHoldout" in value
    ):
        stability = value
        source_kind = "stability_validation"
    else:
        raise ValueError(
            f"{path} is not a stability-validation source"
        )
    return stability, {
        "sourcePath": str(path),
        "sourceKind": source_kind,
        "sourceSha256": _sha256_bytes(raw),
        "stabilitySha256": _sha256_bytes(
            _canonical_bytes(stability)
        ),
    }


def _evidence_excerpt(
    row: dict[str, Any],
    *,
    fields: Iterable[str],
) -> dict[str, Any]:
    return {
        field: row.get(field)
        for field in fields
        if field in row
    }


def extract_policy_candidates(
    stability: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    source = dict(source or {})

    for row in stability.get("featureEffects") or []:
        if (
            row.get("status") != "stable_negative"
            or not row.get("validationCandidate")
        ):
            continue
        dimension = str(row.get("dimension") or "")
        if dimension not in SUPPORTED_FEATURE_DIMENSIONS:
            continue
        signature = {
            "ruleType": PolicyRuleType.BLOCK_FEATURE_VALUE.value,
            "strategy": str(row.get("strategy") or ""),
            "side": str(row.get("side") or ""),
            "regime": str(row.get("regime") or ""),
            "dimension": dimension,
            "value": str(row.get("value") or ""),
        }
        candidates.append({
            "candidateId": _stable_id(
                "candidate",
                signature,
            ),
            **signature,
            "phase": (
                "final"
                if dimension == "confluenceCount"
                else "pre_plan"
            ),
            "validationSource": "featureEffects",
            "validationStatus": row.get("status"),
            "effectDirection": "negative",
            "evidence": _evidence_excerpt(
                row,
                fields=(
                    "selectedSamples",
                    "comparatorSamples",
                    "sessionsWithSelectedValue",
                    "pooledDeltaAllInR",
                    "maxSessionSampleShare",
                    "comparisonSessions",
                    "medianSessionDeltaAllInR",
                    "leaveOneSessionOutFolds",
                    "leaveOneSessionOutSignAgreementRate",
                ),
            ),
            "source": source,
        })

    for row in stability.get(
        "fixedNetRewardRiskThresholds"
    ) or []:
        if (
            row.get("status") != "stable_positive"
            or not row.get("validationCandidate")
        ):
            continue
        threshold = row.get("threshold")
        if not isinstance(threshold, (int, float)):
            continue
        signature = {
            "ruleType": PolicyRuleType.MIN_NET_REWARD_RISK.value,
            "strategy": str(row.get("strategy") or ""),
            "side": str(row.get("side") or ""),
            "regime": str(row.get("regime") or ""),
            "threshold": float(threshold),
        }
        candidates.append({
            "candidateId": _stable_id(
                "candidate",
                {
                    **signature,
                    "validationSource": (
                        "fixedNetRewardRiskThresholds"
                    ),
                },
            ),
            **signature,
            "phase": "post_plan",
            "validationSource": (
                "fixedNetRewardRiskThresholds"
            ),
            "validationStatus": row.get("status"),
            "effectDirection": "positive_gate",
            "evidence": _evidence_excerpt(
                row,
                fields=(
                    "knownSamples",
                    "passSamples",
                    "failSamples",
                    "passExpectancyAllInR",
                    "failExpectancyAllInR",
                    "passMinusFailAllInR",
                    "comparisonSessions",
                    "medianSessionPassMinusFailAllInR",
                    "leaveOneSessionOutFolds",
                    "leaveOneSessionOutSignAgreementRate",
                ),
            ),
            "source": source,
        })

    for row in stability.get(
        "thresholdSelectionHoldout"
    ) or []:
        if (
            row.get("status")
            != "stable_positive_holdout"
            or not row.get("validationCandidate")
        ):
            continue
        counts = row.get("selectedThresholdCounts") or {}
        if not isinstance(counts, dict) or not counts:
            continue
        threshold_text, _ = max(
            counts.items(),
            key=lambda item: (
                int(item[1]),
                -float(item[0]),
            ),
        )
        threshold = float(threshold_text)
        signature = {
            "ruleType": PolicyRuleType.MIN_NET_REWARD_RISK.value,
            "strategy": str(row.get("strategy") or ""),
            "side": str(row.get("side") or ""),
            "regime": str(row.get("regime") or ""),
            "threshold": threshold,
        }
        candidates.append({
            "candidateId": _stable_id(
                "candidate",
                {
                    **signature,
                    "validationSource": (
                        "thresholdSelectionHoldout"
                    ),
                },
            ),
            **signature,
            "phase": "post_plan",
            "validationSource": (
                "thresholdSelectionHoldout"
            ),
            "validationStatus": row.get("status"),
            "effectDirection": "positive_gate",
            "evidence": _evidence_excerpt(
                row,
                fields=(
                    "sessions",
                    "folds",
                    "selectedThresholdCounts",
                    "selectedThresholdModeRate",
                    "holdoutPositiveRate",
                    "meanHoldoutPassMinusFailAllInR",
                    "medianHoldoutPassMinusFailAllInR",
                ),
            ),
            "source": source,
        })

    candidates.sort(
        key=lambda row: (
            str(row.get("strategy") or ""),
            str(row.get("side") or ""),
            str(row.get("regime") or ""),
            str(row.get("ruleType") or ""),
            str(row.get("candidateId") or ""),
        )
    )
    return candidates


def load_policy_candidates(
    source_path: str | Path,
) -> dict[str, Any]:
    stability, source = _read_stability_source(
        source_path
    )
    candidates = extract_policy_candidates(
        stability,
        source=source,
    )
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": source,
        "stabilityPolicy": (
            stability.get("policy") or {}
        ),
        "summary": {
            "candidates": len(candidates),
            "featureBlockCandidates": sum(
                row.get("ruleType")
                == PolicyRuleType.BLOCK_FEATURE_VALUE.value
                for row in candidates
            ),
            "economicThresholdCandidates": sum(
                row.get("ruleType")
                == PolicyRuleType.MIN_NET_REWARD_RISK.value
                for row in candidates
            ),
        },
        "candidates": candidates,
    }


def _rule_from_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    rule_type = str(candidate.get("ruleType") or "")
    scope = {
        "strategy": str(candidate.get("strategy") or ""),
        "side": str(candidate.get("side") or ""),
        "regime": str(candidate.get("regime") or ""),
    }
    if not all(scope.values()):
        raise ValueError(
            f"Candidate lacks exact scope: {candidate}"
        )

    if rule_type == PolicyRuleType.BLOCK_FEATURE_VALUE.value:
        dimension = str(candidate.get("dimension") or "")
        if dimension not in SUPPORTED_FEATURE_DIMENSIONS:
            raise ValueError(
                f"Unsupported feature dimension: {dimension}"
            )
        condition = {
            "dimension": dimension,
            "value": str(candidate.get("value") or ""),
        }
        phase = (
            "final"
            if dimension == "confluenceCount"
            else "pre_plan"
        )
    elif rule_type == PolicyRuleType.MIN_NET_REWARD_RISK.value:
        threshold = candidate.get("threshold")
        if not isinstance(threshold, (int, float)):
            raise ValueError(
                f"Candidate lacks numeric threshold: {candidate}"
            )
        condition = {
            "minimum": float(threshold),
        }
        phase = "post_plan"
    else:
        raise ValueError(
            f"Unsupported rule type: {rule_type}"
        )

    signature = {
        "type": rule_type,
        "scope": scope,
        "condition": condition,
        "phase": phase,
    }
    return {
        "ruleId": _stable_id("rule", signature),
        **signature,
        "effect": "block_on_match",
        "candidateId": candidate["candidateId"],
        "validation": {
            "source": candidate.get(
                "validationSource"
            ),
            "status": candidate.get(
                "validationStatus"
            ),
            "effectDirection": candidate.get(
                "effectDirection"
            ),
            "evidence": candidate.get("evidence") or {},
        },
    }


def load_policy_manifest(
    path: str | Path,
) -> dict[str, Any]:
    policy_path = Path(path)
    value = json.loads(
        policy_path.read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise ValueError(
            f"Expected policy JSON object in {policy_path}"
        )
    _validate_policy_manifest(value)
    return value


def create_policy_manifest(
    catalog: dict[str, Any],
    candidate_ids: Iterable[str],
    *,
    version: int = 1,
    reason: str,
    allow_enforce: bool = False,
    previous_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_ids = list(dict.fromkeys(candidate_ids))
    if not selected_ids:
        raise ValueError(
            "At least one candidate id must be explicitly selected"
        )
    by_id = {
        str(row.get("candidateId")): row
        for row in catalog.get("candidates") or []
    }
    unknown = [
        candidate_id
        for candidate_id in selected_ids
        if candidate_id not in by_id
    ]
    if unknown:
        raise ValueError(
            "Unknown candidate ids: "
            + ", ".join(unknown)
        )
    if (
        previous_policy is not None
        and int(version)
        <= int(previous_policy.get("version") or 0)
    ):
        raise ValueError(
            "New policy version must be greater than previous policy version"
        )
    rules = [
        _rule_from_candidate(by_id[candidate_id])
        for candidate_id in selected_ids
    ]
    source = dict(catalog.get("source") or {})
    rollback = {
        "previousPolicyId": (
            previous_policy.get("policyId")
            if previous_policy
            else None
        ),
        "previousPolicyVersion": (
            previous_policy.get("version")
            if previous_policy
            else None
        ),
    }
    identity = {
        "version": int(version),
        "sourceStabilitySha256": source.get(
            "stabilitySha256"
        ),
        "ruleIds": [
            row["ruleId"]
            for row in rules
        ],
    }
    manifest = {
        "schemaVersion": 1,
        "policyId": _stable_id(
            "research-policy",
            identity,
            length=20,
        ),
        "version": int(version),
        "createdAt": datetime.now(UTC).isoformat(),
        "status": "promoted",
        "allowEnforce": bool(allow_enforce),
        "source": source,
        "audit": {
            "promotionReason": str(reason),
            "candidateIds": selected_ids,
            "candidateCount": len(selected_ids),
            "automaticPromotion": False,
            "livePolicyEligibleAtSource": False,
        },
        "rollback": rollback,
        "rules": rules,
    }
    manifest["policyFingerprint"] = _sha256_bytes(
        _canonical_bytes(
            _policy_fingerprint_payload(manifest)
        )
    )
    _validate_policy_manifest(manifest)
    return manifest


def write_policy_manifest(
    path: str | Path,
    manifest: dict[str, Any],
) -> Path:
    _validate_policy_manifest(manifest)
    output = Path(path)
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    output.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output


def _validate_policy_manifest(
    manifest: dict[str, Any],
) -> None:
    if int(manifest.get("schemaVersion") or 0) != 1:
        raise ValueError(
            "Unsupported research policy schema"
        )
    if not manifest.get("policyId"):
        raise ValueError("Policy id is required")
    if int(manifest.get("version") or 0) <= 0:
        raise ValueError(
            "Policy version must be positive"
        )
    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(
            "Policy must contain at least one rule"
        )
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(
                "Policy rule must be an object"
            )
        rule_id = str(rule.get("ruleId") or "")
        if not rule_id or rule_id in seen:
            raise ValueError(
                "Policy rule ids must be unique"
            )
        seen.add(rule_id)
        rule_type = str(rule.get("type") or "")
        if rule_type not in {
            value.value
            for value in PolicyRuleType
        }:
            raise ValueError(
                f"Unsupported rule type: {rule_type}"
            )
        phase = str(rule.get("phase") or "")
        if phase not in {
            "pre_plan",
            "post_plan",
            "final",
        }:
            raise ValueError(
                f"Unsupported policy phase: {phase}"
            )
        scope = rule.get("scope")
        if not isinstance(scope, dict):
            raise ValueError(
                "Policy rule scope is required"
            )
        for key in (
            "strategy",
            "side",
            "regime",
        ):
            if not str(scope.get(key) or ""):
                raise ValueError(
                    f"Policy exact scope lacks {key}"
                )

    fingerprint = str(
        manifest.get("policyFingerprint") or ""
    )
    if fingerprint:
        expected = _sha256_bytes(
            _canonical_bytes(
                _policy_fingerprint_payload(
                    manifest
                )
            )
        )
        if fingerprint != expected:
            raise ValueError(
                "Research policy fingerprint mismatch"
            )


def _mode(value: str | PolicyMode) -> PolicyMode:
    if isinstance(value, PolicyMode):
        return value
    try:
        return PolicyMode(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(
            "research policy mode must be off, shadow, or enforce"
        ) from exc


@dataclass(frozen=True, slots=True)
class PolicyAssessment:
    policy_id: str | None
    policy_version: int | None
    policy_fingerprint: str | None
    mode: PolicyMode
    phase: str
    strategy: str
    side: str
    regime: str
    matched_rule_ids: tuple[str, ...]
    matched_rules: tuple[dict[str, Any], ...]
    would_block: bool
    blocked: bool
    reasons: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "policyFingerprint": (
                self.policy_fingerprint
            ),
            "mode": self.mode.value,
            "phase": self.phase,
            "strategy": self.strategy,
            "side": self.side,
            "regime": self.regime,
            "matchedRuleIds": list(
                self.matched_rule_ids
            ),
            "matchedRules": [
                dict(row)
                for row in self.matched_rules
            ],
            "wouldBlock": self.would_block,
            "blocked": self.blocked,
            "reasons": list(self.reasons),
        }


class ResearchPolicyRuntime:
    def __init__(
        self,
        *,
        mode: str | PolicyMode = PolicyMode.OFF,
        manifest: dict[str, Any] | None = None,
        source_path: str | None = None,
        source_sha256: str | None = None,
    ) -> None:
        self.mode = _mode(mode)
        self.manifest = manifest
        self.source_path = source_path
        self.source_sha256 = source_sha256
        if self.mode != PolicyMode.OFF:
            if manifest is None:
                raise ValueError(
                    "research policy file is required when mode is not off"
                )
            _validate_policy_manifest(manifest)
            if (
                self.mode == PolicyMode.ENFORCE
                and not bool(
                    manifest.get("allowEnforce")
                )
            ):
                raise ValueError(
                    "policy manifest does not allow enforce mode"
                )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        mode: str | PolicyMode,
    ) -> "ResearchPolicyRuntime":
        resolved_mode = _mode(mode)
        if resolved_mode == PolicyMode.OFF:
            return cls(mode=resolved_mode)
        policy_path = Path(path)
        if not policy_path.is_file():
            raise FileNotFoundError(policy_path)
        raw = policy_path.read_bytes()
        manifest = json.loads(
            raw.decode("utf-8")
        )
        return cls(
            mode=resolved_mode,
            manifest=manifest,
            source_path=str(policy_path),
            source_sha256=_sha256_bytes(raw),
        )

    @classmethod
    def from_settings(
        cls,
        *,
        path: str,
        mode: str,
    ) -> "ResearchPolicyRuntime":
        resolved_mode = _mode(mode)
        if resolved_mode == PolicyMode.OFF:
            return cls(mode=resolved_mode)
        if not path:
            raise ValueError(
                "SCALP_RESEARCH_POLICY_FILE is required "
                "when research policy mode is shadow/enforce"
            )
        return cls.from_file(
            path,
            mode=resolved_mode,
        )

    @property
    def active(self) -> bool:
        return (
            self.mode != PolicyMode.OFF
            and self.manifest is not None
        )

    def public(self) -> dict[str, Any]:
        manifest = self.manifest or {}
        return {
            "mode": self.mode.value,
            "active": self.active,
            "policyId": manifest.get(
                "policyId"
            ),
            "version": manifest.get(
                "version"
            ),
            "policyFingerprint": manifest.get(
                "policyFingerprint"
            ),
            "allowEnforce": bool(
                manifest.get("allowEnforce")
            ),
            "sourcePath": self.source_path,
            "sourceSha256": self.source_sha256,
            "rollback": manifest.get(
                "rollback"
            ),
            "rules": len(
                manifest.get("rules") or []
            ),
        }

    @staticmethod
    def _scope(
        decision: StrategyDecision,
        context: MarketContext | None,
    ) -> tuple[str, str, str]:
        strategy = decision.strategy
        side = decision.action.value
        details = decision.details or {}
        decision_context = details.get(
            "decisionContext"
        )
        if isinstance(decision_context, dict):
            regime = str(
                decision_context.get(
                    "localRegime"
                )
                or "unknown"
            )
        elif (
            context is not None
            and context.local_regime is not None
        ):
            regime = context.local_regime.regime.value
        else:
            regime = "unknown"
        return strategy, side, regime

    @staticmethod
    def _feature_value(
        decision: StrategyDecision,
        dimension: str,
        arbitration: (
            SemanticCandidateAssessment
            | dict[str, Any]
            | None
        ),
    ) -> str | None:
        details = decision.details or {}
        if dimension == "flowAlignment":
            value = details.get(
                "flowAlignment"
            )
            if isinstance(value, dict):
                return str(
                    value.get(
                        "classification"
                    )
                    or "unknown"
                )
        elif dimension == "entryFreshness":
            value = details.get(
                "entryFreshness"
            )
            if isinstance(value, dict):
                return str(
                    value.get(
                        "classification"
                    )
                    or "unknown"
                )
        elif dimension == "liquidityAlignment":
            value = details.get(
                "liquidityAlignment"
            )
            if isinstance(value, dict):
                return str(
                    value.get(
                        "classification"
                    )
                    or "unknown"
                )
        elif dimension == "confluenceCount":
            if isinstance(
                arbitration,
                SemanticCandidateAssessment,
            ):
                return str(
                    arbitration.confluence_count
                )
            if isinstance(arbitration, dict):
                return str(
                    int(
                        arbitration.get(
                            "confluenceCount"
                        )
                        or 0
                    )
                )
        return None

    @staticmethod
    def _rule_matches_scope(
        rule: dict[str, Any],
        *,
        strategy: str,
        side: str,
        regime: str,
    ) -> bool:
        scope = rule.get("scope") or {}
        return (
            str(scope.get("strategy") or "")
            == strategy
            and str(scope.get("side") or "")
            == side
            and str(scope.get("regime") or "")
            == regime
        )

    def evaluate(
        self,
        decision: StrategyDecision,
        context: MarketContext | None,
        *,
        phase: str,
        plan: TradePlan | None = None,
        arbitration: (
            SemanticCandidateAssessment
            | dict[str, Any]
            | None
        ) = None,
    ) -> PolicyAssessment:
        strategy, side, regime = self._scope(
            decision,
            context,
        )
        if not self.active:
            return PolicyAssessment(
                policy_id=None,
                policy_version=None,
                policy_fingerprint=None,
                mode=self.mode,
                phase=phase,
                strategy=strategy,
                side=side,
                regime=regime,
                matched_rule_ids=(),
                matched_rules=(),
                would_block=False,
                blocked=False,
                reasons=(),
            )

        matched: list[dict[str, Any]] = []
        reasons: list[str] = []
        for rule in (
            self.manifest.get("rules") or []
        ):
            if str(rule.get("phase") or "") != phase:
                continue
            if not self._rule_matches_scope(
                rule,
                strategy=strategy,
                side=side,
                regime=regime,
            ):
                continue

            rule_type = str(
                rule.get("type") or ""
            )
            condition = rule.get(
                "condition"
            ) or {}
            if (
                rule_type
                == PolicyRuleType.BLOCK_FEATURE_VALUE.value
            ):
                dimension = str(
                    condition.get(
                        "dimension"
                    )
                    or ""
                )
                expected = str(
                    condition.get("value") or ""
                )
                actual = self._feature_value(
                    decision,
                    dimension,
                    arbitration,
                )
                if (
                    actual is not None
                    and actual == expected
                ):
                    matched.append(rule)
                    reasons.append(
                        f"{dimension}={actual} matched "
                        f"promoted negative feature rule"
                    )
            elif (
                rule_type
                == PolicyRuleType.MIN_NET_REWARD_RISK.value
            ):
                if plan is None:
                    continue
                minimum = condition.get(
                    "minimum"
                )
                if not isinstance(
                    minimum,
                    (int, float),
                ):
                    continue
                if (
                    float(plan.net_reward_risk)
                    < float(minimum)
                ):
                    matched.append(rule)
                    reasons.append(
                        "planned net R:R "
                        f"{float(plan.net_reward_risk):.3f} "
                        f"< promoted minimum "
                        f"{float(minimum):.3f}"
                    )

        would_block = bool(matched)
        blocked = (
            would_block
            and self.mode == PolicyMode.ENFORCE
        )
        manifest = self.manifest or {}
        return PolicyAssessment(
            policy_id=str(
                manifest.get("policyId") or ""
            ),
            policy_version=int(
                manifest.get("version") or 0
            ),
            policy_fingerprint=str(
                manifest.get(
                    "policyFingerprint"
                )
                or ""
            ),
            mode=self.mode,
            phase=phase,
            strategy=strategy,
            side=side,
            regime=regime,
            matched_rule_ids=tuple(
                str(rule.get("ruleId") or "")
                for rule in matched
            ),
            matched_rules=tuple(
                {
                    "ruleId": rule.get(
                        "ruleId"
                    ),
                    "type": rule.get("type"),
                    "scope": rule.get("scope"),
                    "condition": rule.get(
                        "condition"
                    ),
                    "candidateId": rule.get(
                        "candidateId"
                    ),
                    "validation": rule.get(
                        "validation"
                    ),
                }
                for rule in matched
            ),
            would_block=would_block,
            blocked=blocked,
            reasons=tuple(reasons),
        )
