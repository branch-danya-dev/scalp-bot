import json

import pytest

from scalp_bot.domain import Action, Side, StrategyDecision, TradePlan
from scalp_bot.research_policy import (
    PolicyMode,
    ResearchPolicyRuntime,
    create_policy_manifest,
    extract_policy_candidates,
    load_policy_candidates,
    load_policy_manifest,
    write_policy_manifest,
)


def stability_fixture() -> dict:
    return {
        "schemaVersion": 1,
        "policy": {
            "minimumSessions": 4,
            "livePolicyEnforcement": "disabled",
        },
        "featureEffects": [
            {
                "strategy": "trend_structure",
                "side": "long",
                "regime": "bullish_trend",
                "dimension": "flowAlignment",
                "value": "short_term_reversal",
                "status": "stable_negative",
                "validationCandidate": True,
                "selectedSamples": 20,
                "comparatorSamples": 30,
                "sessionsWithSelectedValue": 5,
                "pooledDeltaAllInR": -0.35,
                "maxSessionSampleShare": 0.25,
                "comparisonSessions": 5,
                "medianSessionDeltaAllInR": -0.30,
                "leaveOneSessionOutFolds": 5,
                "leaveOneSessionOutSignAgreementRate": 0.8,
            },
            {
                "strategy": "trend_structure",
                "side": "long",
                "regime": "bullish_trend",
                "dimension": "liquidityAlignment",
                "value": "opposed",
                "status": "unstable",
                "validationCandidate": False,
            },
            {
                "strategy": "level_breakout",
                "side": "long",
                "regime": "range",
                "dimension": "confluenceCount",
                "value": "0",
                "status": "stable_negative",
                "validationCandidate": True,
                "selectedSamples": 18,
                "comparatorSamples": 12,
                "sessionsWithSelectedValue": 4,
                "pooledDeltaAllInR": -0.20,
                "maxSessionSampleShare": 0.30,
                "comparisonSessions": 4,
                "medianSessionDeltaAllInR": -0.18,
                "leaveOneSessionOutFolds": 4,
                "leaveOneSessionOutSignAgreementRate": 1.0,
            },
        ],
        "fixedNetRewardRiskThresholds": [
            {
                "strategy": "trend_structure",
                "side": "long",
                "regime": "bullish_trend",
                "threshold": 1.15,
                "status": "stable_positive",
                "validationCandidate": True,
                "knownSamples": 40,
                "passSamples": 20,
                "failSamples": 20,
                "passExpectancyAllInR": 0.25,
                "failExpectancyAllInR": -0.20,
                "passMinusFailAllInR": 0.45,
                "comparisonSessions": 5,
                "medianSessionPassMinusFailAllInR": 0.30,
                "leaveOneSessionOutFolds": 5,
                "leaveOneSessionOutSignAgreementRate": 0.8,
            }
        ],
        "thresholdSelectionHoldout": [
            {
                "strategy": "weak_level_rejection",
                "side": "short",
                "regime": "bearish_trend",
                "status": "stable_positive_holdout",
                "validationCandidate": True,
                "sessions": 5,
                "folds": 5,
                "selectedThresholdCounts": {
                    "0.75": 1,
                    "1.00": 4,
                },
                "selectedThresholdModeRate": 0.8,
                "holdoutPositiveRate": 0.8,
                "meanHoldoutPassMinusFailAllInR": 0.25,
                "medianHoldoutPassMinusFailAllInR": 0.20,
            }
        ],
    }


def decision(
    *,
    strategy="trend_structure",
    side=Action.LONG,
    regime="bullish_trend",
    flow="short_term_reversal",
    freshness="fresh",
    liquidity="neutral",
) -> StrategyDecision:
    return StrategyDecision(
        strategy=strategy,
        action=side,
        reasons=["ready"],
        confidence=0.8,
        entry=100.0,
        stop=99.5 if side == Action.LONG else 100.5,
        target=101.0 if side == Action.LONG else 99.0,
        setup_id="setup-1",
        details={
            "decisionContext": {
                "localRegime": regime,
            },
            "flowAlignment": {
                "classification": flow,
            },
            "entryFreshness": {
                "classification": freshness,
            },
            "liquidityAlignment": {
                "classification": liquidity,
            },
        },
    )


def plan(
    *,
    strategy="trend_structure",
    side=Side.LONG,
    net_rr=0.9,
) -> TradePlan:
    return TradePlan(
        symbol="AAAUSDT",
        strategy=strategy,
        side=side,
        setup_entry=100.0,
        market_entry=100.0,
        stop=99.5 if side == Side.LONG else 100.5,
        target=101.0 if side == Side.LONG else 99.0,
        notional=1000.0,
        leverage=1.0,
        max_loss_usd=5.0,
        expected_gross_profit=10.0,
        estimated_costs=1.0,
        expected_net_profit=9.0,
        expected_net_loss=5.0,
        net_reward_risk=net_rr,
        entry_drift_pct=0.0,
        setup_id="setup-1",
        strategy_details={},
    )


def test_candidate_extraction_requires_stable_stage11_evidence() -> None:
    candidates = extract_policy_candidates(
        stability_fixture(),
        source={
            "stabilitySha256": "abc",
        },
    )

    signatures = {
        (
            row["ruleType"],
            row["strategy"],
            row["side"],
            row["regime"],
            row.get("dimension"),
            row.get("threshold"),
        )
        for row in candidates
    }
    assert (
        "block_feature_value",
        "trend_structure",
        "long",
        "bullish_trend",
        "flowAlignment",
        None,
    ) in signatures
    assert (
        "block_feature_value",
        "level_breakout",
        "long",
        "range",
        "confluenceCount",
        None,
    ) in signatures
    assert (
        "min_net_reward_risk",
        "trend_structure",
        "long",
        "bullish_trend",
        None,
        1.15,
    ) in signatures
    assert (
        "min_net_reward_risk",
        "weak_level_rejection",
        "short",
        "bearish_trend",
        None,
        1.0,
    ) in signatures
    assert all(
        not (
            row.get("dimension")
            == "liquidityAlignment"
            and row.get("value") == "opposed"
        )
        for row in candidates
    )


def test_policy_promotion_requires_explicit_candidate_selection() -> None:
    catalog = {
        "source": {
            "stabilitySha256": "abc",
        },
        "candidates": extract_policy_candidates(
            stability_fixture(),
        ),
    }

    with pytest.raises(ValueError):
        create_policy_manifest(
            catalog,
            [],
            version=1,
            reason="test",
        )


def test_policy_manifest_records_version_audit_and_rollback() -> None:
    catalog = {
        "source": {
            "stabilitySha256": "abc",
        },
        "candidates": extract_policy_candidates(
            stability_fixture(),
        ),
    }
    first_candidate = catalog["candidates"][0]
    previous = create_policy_manifest(
        catalog,
        [first_candidate["candidateId"]],
        version=1,
        reason="initial shadow",
    )
    next_candidate = catalog["candidates"][-1]
    manifest = create_policy_manifest(
        catalog,
        [next_candidate["candidateId"]],
        version=2,
        reason="validated replacement",
        allow_enforce=True,
        previous_policy=previous,
    )

    assert manifest["version"] == 2
    assert manifest["allowEnforce"] is True
    assert manifest["audit"]["automaticPromotion"] is False
    assert manifest["audit"]["promotionReason"] == "validated replacement"
    assert manifest["rollback"]["previousPolicyId"] == previous["policyId"]
    assert manifest["rollback"]["previousPolicyVersion"] == 1
    assert manifest["policyFingerprint"]

    with pytest.raises(ValueError):
        create_policy_manifest(
            catalog,
            [next_candidate["candidateId"]],
            version=1,
            reason="bad rollback version",
            previous_policy=previous,
        )


def test_shadow_feature_rule_records_would_block_without_blocking() -> None:
    catalog = {
        "source": {"stabilitySha256": "abc"},
        "candidates": extract_policy_candidates(
            stability_fixture(),
        ),
    }
    candidate = next(
        row
        for row in catalog["candidates"]
        if (
            row.get("dimension") == "flowAlignment"
            and row.get("value") == "short_term_reversal"
        )
    )
    manifest = create_policy_manifest(
        catalog,
        [candidate["candidateId"]],
        version=1,
        reason="shadow validation",
    )
    runtime = ResearchPolicyRuntime(
        mode=PolicyMode.SHADOW,
        manifest=manifest,
    )

    assessment = runtime.evaluate(
        decision(),
        None,
        phase="pre_plan",
    )

    assert assessment.would_block is True
    assert assessment.blocked is False
    assert assessment.mode == PolicyMode.SHADOW
    assert assessment.matched_rule_ids


def test_enforce_requires_manifest_allow_enforce() -> None:
    catalog = {
        "source": {"stabilitySha256": "abc"},
        "candidates": extract_policy_candidates(
            stability_fixture(),
        ),
    }
    candidate = catalog["candidates"][0]
    manifest = create_policy_manifest(
        catalog,
        [candidate["candidateId"]],
        version=1,
        reason="shadow only",
        allow_enforce=False,
    )

    with pytest.raises(ValueError):
        ResearchPolicyRuntime(
            mode=PolicyMode.ENFORCE,
            manifest=manifest,
        )


def test_enforced_rr_rule_blocks_only_exact_scope_below_threshold() -> None:
    catalog = {
        "source": {"stabilitySha256": "abc"},
        "candidates": extract_policy_candidates(
            stability_fixture(),
        ),
    }
    candidate = next(
        row
        for row in catalog["candidates"]
        if (
            row["ruleType"] == "min_net_reward_risk"
            and row["strategy"] == "trend_structure"
            and row["side"] == "long"
            and row["regime"] == "bullish_trend"
            and row["validationSource"]
            == "fixedNetRewardRiskThresholds"
        )
    )
    manifest = create_policy_manifest(
        catalog,
        [candidate["candidateId"]],
        version=1,
        reason="enforce validated rr",
        allow_enforce=True,
    )
    runtime = ResearchPolicyRuntime(
        mode=PolicyMode.ENFORCE,
        manifest=manifest,
    )

    blocked = runtime.evaluate(
        decision(),
        None,
        phase="post_plan",
        plan=plan(net_rr=0.9),
    )
    passed = runtime.evaluate(
        decision(),
        None,
        phase="post_plan",
        plan=plan(net_rr=1.3),
    )
    other_regime = runtime.evaluate(
        decision(regime="range"),
        None,
        phase="post_plan",
        plan=plan(net_rr=0.9),
    )

    assert blocked.blocked is True
    assert passed.would_block is False
    assert other_regime.would_block is False


def test_final_phase_can_enforce_promoted_confluence_rule() -> None:
    catalog = {
        "source": {"stabilitySha256": "abc"},
        "candidates": extract_policy_candidates(
            stability_fixture(),
        ),
    }
    candidate = next(
        row
        for row in catalog["candidates"]
        if row.get("dimension") == "confluenceCount"
    )
    manifest = create_policy_manifest(
        catalog,
        [candidate["candidateId"]],
        version=1,
        reason="no-confluence is stable negative",
        allow_enforce=True,
    )
    runtime = ResearchPolicyRuntime(
        mode="enforce",
        manifest=manifest,
    )
    breakout = decision(
        strategy="level_breakout",
        side=Action.LONG,
        regime="range",
        flow="aligned",
    )

    assessment = runtime.evaluate(
        breakout,
        None,
        phase="final",
        arbitration={
            "confluenceCount": 0,
        },
    )

    assert assessment.blocked is True
    assert assessment.matched_rules[0]["condition"] == {
        "dimension": "confluenceCount",
        "value": "0",
    }


def test_policy_file_integrity_detects_manual_rule_tampering(tmp_path) -> None:
    stability_path = tmp_path / "stability.json"
    stability_path.write_text(
        json.dumps(stability_fixture()),
        encoding="utf-8",
    )
    catalog = load_policy_candidates(
        stability_path
    )
    candidate = catalog["candidates"][0]
    manifest = create_policy_manifest(
        catalog,
        [candidate["candidateId"]],
        version=1,
        reason="integrity test",
    )
    policy_path = tmp_path / "policy.json"
    write_policy_manifest(
        policy_path,
        manifest,
    )

    loaded = load_policy_manifest(policy_path)
    assert loaded["policyFingerprint"] == manifest["policyFingerprint"]

    tampered = dict(loaded)
    tampered["rules"] = [
        dict(row)
        for row in loaded["rules"]
    ]
    tampered["rules"][0] = dict(
        tampered["rules"][0]
    )
    tampered["rules"][0]["condition"] = {
        "dimension": "flowAlignment",
        "value": "strongly_aligned",
    }
    policy_path.write_text(
        json.dumps(tampered),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="fingerprint mismatch",
    ):
        load_policy_manifest(policy_path)
