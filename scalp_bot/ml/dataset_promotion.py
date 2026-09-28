"""Preregistered population gate. Counts/coverage only; never selects by returns."""
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class DatasetPolicy:
    version: str = "wave2-population-20260928-v1"
    min_prepared: int = 2000
    min_complete_labels: int = 1000
    min_per_strategy: int = 200
    min_symbols: int = 6
    min_independent_captures: int = 6
    min_utc_days: int = 3
    min_capture_duration_ms: int = 600_000
    period_separation_ms: int = 180_000
    max_symbol_share: float = .30
    max_capture_share: float = .25
    min_loso_symbols: int = 4
    min_labels_per_loso_symbol: int = 100
    min_captures_per_loso_symbol: int = 3
    min_categories_per_context: int = 2
    min_labels_per_context_category: int = 100
    max_context_missing_fraction: float = .10
    max_censored_fraction: float = .35
    required_fill_depth_provenance_ratio: float = 1.


POLICY = DatasetPolicy()
POLICY_HASH = digest(asdict(POLICY))
CONTEXT_FIELDS = ("localRegime", "htfAlignment", "flowAlignment")
UNKNOWN = (None, "", "unknown", "unavailable", "insufficient_data")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def assess_population(rows, captures):
    """Recompute from the complete observation population, including rejections.

    `captures` contains chain/replay receipts and UTC capture intervals. No
    monotonic values are compared across runs. Overlapping/adjacent captures
    are one independent period, not multiple independent observations.
    """
    reasons, identities, complete, eligible_count, censored = [], set(), [], 0, 0
    rejected = 0
    for row in rows:
        capture, identity = row.get("capture_id"), row.get("identity")
        if not capture or not identity or (capture, identity) in identities:
            reasons.append("missing_or_duplicate_identity")
        identities.add((capture, identity))
        source = row.get("source", {})
        if (source.get("capture_id") != capture or source.get("clock_domain") != "capture:"+str(capture)
                or source.get("symbol") != row.get("symbol")):
            reasons.append("source_domain_or_symbol")
        start, end = row.get("available_wall_ms"), row.get("label_end_wall_ms")
        proof = captures.get(capture, {})
        left, right = proof.get("startWallMs"), proof.get("endWallMs")
        if (not all(_finite(v) for v in (start, left, right)) or not left <= start <= right
                or row.get("wall_time_provenance") != "local_utc_at_preparation"):
            reasons.append("global_wall_provenance")
        if row.get("economicsAllowed") is False:
            rejected += 1
            if row.get("realized_net_r") is not None or row.get("trainingReady"):
                reasons.append("rejected_intent_has_execution_label")
            continue
        if row.get("economicsAllowed") is not True:
            reasons.append("missing_economic_disposition")
        eligible_count += 1
        if row.get("censor_reason") or not row.get("trainingReady"):
            censored += 1
            if row.get("realized_net_r") is not None:
                reasons.append("censored_return_substitute")
            continue
        if (not all(_finite(v) for v in (start, end, row.get("realized_net_r"))) or end < start
                or not _finite(right) or end > right or type(row.get("target_before_stop")) is not bool):
            reasons.append("invalid_complete_label")
            continue
        complete.append(row)
    n = len(complete)
    symbols = Counter(r.get("symbol") for r in complete)
    capture_counts = Counter(r["capture_id"] for r in complete)
    strategies = Counter(r.get("strategy") for r in complete)
    checks = dict(prepared=len(rows) >= POLICY.min_prepared,
        complete=n >= POLICY.min_complete_labels,
        strategies=all(strategies[k] >= POLICY.min_per_strategy for k in ("level_breakout", "weak_level_rejection")),
        symbols=len(symbols) >= POLICY.min_symbols,
        symbol_concentration=bool(n) and max(symbols.values())/n <= POLICY.max_symbol_share,
        capture_concentration=bool(n) and max(capture_counts.values())/n <= POLICY.max_capture_share,
        censor_rate=bool(eligible_count) and censored/eligible_count <= POLICY.max_censored_fraction)
    periods, days = [], set()
    for capture in sorted(capture_counts):
        proof = captures.get(capture, {})
        left, right = proof.get("startWallMs"), proof.get("endWallMs")
        if (proof.get("primaryIntegrity") != "MET" or proof.get("labelReplay") != "MET"
                or proof.get("populationComplete") is not True):
            reasons.append("capture_integrity_replay_or_population")
        if not all(_finite(v) for v in (left, right)) or right-left < POLICY.min_capture_duration_ms:
            reasons.append("capture_interval")
            continue
        periods.append((left, right))
        try:
            days.add(datetime.fromtimestamp(left/1000, timezone.utc).date().isoformat())
        except (OverflowError, OSError, ValueError):
            reasons.append("invalid_utc_date")
    merged = []
    for left, right in sorted(periods):
        if merged and left <= merged[-1][1]+POLICY.period_separation_ms:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    checks["independent_periods"] = len(merged) >= POLICY.min_independent_captures
    checks["utc_days"] = len(days) >= POLICY.min_utc_days
    supported = [s for s, count in symbols.items() if count >= POLICY.min_labels_per_loso_symbol
        and len({r["capture_id"] for r in complete if r.get("symbol") == s}) >= POLICY.min_captures_per_loso_symbol]
    checks["loso_support"] = len(supported) >= POLICY.min_loso_symbols
    contexts = {}
    for field in CONTEXT_FIELDS:
        counts = Counter(r.get("segment", {}).get(field) for r in complete)
        missing = sum(counts.get(v, 0) for v in UNKNOWN)
        contexts[field] = dict(counts={str(k):v for k,v in counts.items()}, missing=missing)
        checks[field] = (bool(n) and missing/n <= POLICY.max_context_missing_fraction
            and sum(k not in UNKNOWN and v >= POLICY.min_labels_per_context_category for k,v in counts.items())
                >= POLICY.min_categories_per_context)
    provenance = 0
    for row in complete:
        p = row.get("cost_fill_provenance", {})
        plan = p.get("frozenPlan")
        if (p.get("executor") == "PaperBroker" and p.get("events") and p.get("trade")
                and p.get("entryDepth") and p.get("instrument") and plan
                and row.get("planHash") == digest(plan) and row.get("sourcePlanIdentity") == digest([row["source"], plan])):
            provenance += 1
    ratio = provenance/n if n else 0
    checks["fill_depth_provenance"] = bool(n) and ratio >= POLICY.required_fill_depth_provenance_ratio
    reasons.extend(k for k,v in checks.items() if not v)
    return dict(schema="dataset-promotion-receipt-v1", policy=asdict(POLICY), policyHash=POLICY_HASH,
        datasetHash=digest(rows), status="MET" if not reasons else "NOT_MET", reasons=sorted(set(reasons)),
        counts=dict(prepared=len(rows), complete=n, censored=censored, economicRejected=rejected,
            economicallyEligible=eligible_count, symbols=dict(symbols), captures=dict(capture_counts),
            strategies=dict(strategies), independentPeriods=len(merged), utcDays=len(days)),
        contexts=contexts, losoSymbols=supported, provenanceRatio=ratio, checks=checks,
        externalCoverageRequired=False, promotionAuthorized=False)


def require_population(rows, evidence):
    if evidence.get("populationPolicyHash") != POLICY_HASH:
        raise ValueError("dataset promotion gate: missing/mismatched preregistered policy")
    receipt = assess_population(rows, evidence.get("captures", {}))
    if receipt["status"] != "MET":
        raise ValueError("dataset promotion gate: "+", ".join(receipt["reasons"]))
    return receipt
