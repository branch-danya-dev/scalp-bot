# Stage 12 — Research-to-policy promotion framework

## Goal

Stage 11 can identify research candidates that remain stable across independent session holdouts.

Stage 12 defines the only supported path from those research results into an opt-in live rule.

It deliberately does not provide automatic self-modification.

## Promotion boundary

A Stage 11 result is eligible to appear in the promotion catalog only when it already passed the corresponding stability test.

Currently supported candidates are:

- `stable_negative` feature effects -> `block_feature_value`;
- `stable_positive` fixed net-R:R thresholds -> `min_net_reward_risk`;
- `stable_positive_holdout` threshold-selection results -> `min_net_reward_risk` using the modal validated threshold.

Unsupported/unstable rows are not emitted as promotable candidates.

Supported feature dimensions are:

- `flowAlignment`;
- `entryFreshness`;
- `liquidityAlignment`;
- `confluenceCount`.

Each candidate is exact-scope:

`strategy × side × LocalRegime`

There is no global fallback from one side/regime into another.

## No automatic promotion

`research-dataset-*.zip` now includes:

`policy-candidates.json`

and the same catalog is embedded as:

`cross-session-report.json -> policyPromotionCandidates`

A candidate only becomes a policy rule when its candidate id is explicitly selected by the promotion command.

Listing candidates does not create or activate a policy:

```powershell
.\.venv\Scripts\python.exe .\scripts\promote-research-policy.py data\sessions\research-dataset-YYYYMMDDTHHMMSSZ.zip
```

Example explicit promotion into a shadow-only manifest:

```powershell
.\.venv\Scripts\python.exe .\scripts\promote-research-policy.py `
  data\sessions\research-dataset-YYYYMMDDTHHMMSSZ.zip `
  --candidate candidate-0123456789abcdef `
  --output config\policies\research-policy-v1.json `
  --version 1 `
  --reason "short_term_reversal remained negative across independent session holdouts"
```

Multiple candidate ids may be promoted by repeating `--candidate`.

## Versioned policy manifest

Every promoted policy contains:

- schema version;
- policy id;
- monotonically increasing version;
- creation timestamp;
- source dataset/stability hashes;
- explicit human promotion reason;
- exact candidate ids;
- individual rule ids;
- validation evidence excerpt for every rule;
- `allowEnforce` flag;
- rollback metadata;
- policy fingerprint.

The fingerprint covers the policy id/version, enforcement permission, provenance, rollback target and all rules.

If the JSON is edited after promotion without rebuilding the manifest, runtime loading fails with a fingerprint mismatch.

When `--previous-policy` is supplied, the new version must be greater than the previous policy version and the previous id/version are recorded as the rollback target.

## Runtime modes

Runtime is controlled independently from the manifest:

```text
SCALP_RESEARCH_POLICY_MODE=off|shadow|enforce
SCALP_RESEARCH_POLICY_FILE=config/policies/research-policy-v1.json
```

Default is:

`off`

### off

The policy file is ignored and trading behavior is unchanged.

### shadow

The runtime evaluates the promoted rules, records every unique would-block match, but never stops the trade because of the research policy.

Shadow mode is the expected first activation mode for every new promoted policy.

### enforce

A matching promoted rule can block the candidate.

Enforce mode requires two independent explicit actions:

1. the manifest must have been created with `--allow-enforce`;
2. runtime must be started with `SCALP_RESEARCH_POLICY_MODE=enforce`.

If either condition is missing, enforce mode does not start.

Example enforce-capable manifest creation:

```powershell
.\.venv\Scripts\python.exe .\scripts\promote-research-policy.py `
  data\sessions\research-dataset-YYYYMMDDTHHMMSSZ.zip `
  --candidate candidate-0123456789abcdef `
  --output config\policies\research-policy-v2.json `
  --version 2 `
  --previous-policy config\policies\research-policy-v1.json `
  --allow-enforce `
  --reason "candidate remained stable during subsequent shadow paper-runs"
```

## Rule phases

Rules are evaluated only where their information is causally available.

### pre_plan

Feature blocks based on:

- flow alignment;
- EntryFreshness;
- liquidity alignment.

They run after semantic candidate validation but before RiskEngine plan construction.

### post_plan

`min_net_reward_risk` runs only after RiskEngine has produced the actual plan and planned net R:R.

### final

`confluenceCount` rules run only after the semantic arbiter has computed final same-direction confluence/conflict state.

This phase separation prevents a promoted rule from using information that did not yet exist at that point in the live decision pipeline.

## Audit trail

`bot_started.config.researchPolicy` records the active runtime mode and policy identity.

When a run starts with an active policy, the session records:

`research_policy_activated`

Matched rules produce one deduplicated event per setup/phase/matched-rule set:

- `research_policy_shadow`;
- `research_policy_blocked`.

Each event contains:

- policy id/version/fingerprint;
- runtime mode;
- strategy/side/regime;
- decision phase;
- matched rule ids;
- promoted validation evidence;
- would-block / blocked result.

Session reports aggregate these under:

`researchPolicyAudit`

and strategy diagnostics expose:

- `researchPolicyShadowMatches`;
- `researchPolicyBlockedMatches`;
- `researchPolicyRuleMatchCounts`.

Analysis packs preserve deeper DOM around shadow/enforcement events.

## Rollback

Rollback does not require code changes.

Fastest safety rollback:

```text
SCALP_RESEARCH_POLICY_MODE=off
```

Version rollback:

point `SCALP_RESEARCH_POLICY_FILE` back to the previous versioned policy file.

When policies are created with `--previous-policy`, the current manifest records the previous policy id/version so the intended rollback target is auditable.

Policy files should be kept immutable and versioned rather than overwritten.

## Supported live effects

Stage 12 intentionally supports only simple veto-style rules:

### block_feature_value

Example:

`trend_structure × LONG × bullish_trend × flowAlignment=short_term_reversal -> block`

### min_net_reward_risk

Example:

`weak_level_rejection × SHORT × bearish_trend -> require planned net R:R >= 1.00`

Stage 12 does not change stops, targets, sizing, leverage, strategy parameters or ranking weights.

Those would require separate research and a new explicit policy type.

## Promotion lifecycle

Recommended lifecycle:

```text
research telemetry
-> cross-session dataset
-> Stage 11 holdout validation
-> candidate catalog
-> explicit human promotion
-> shadow paper-runs
-> compare shadow blocks vs realized counterfactual
-> optional new enforce-capable version
-> enforce paper-runs
-> continued holdout monitoring
-> rollback/off if effect degrades
```

## Safety invariant

No Stage 11 or Stage 12 result is self-activating.

`validationCandidate=true` means only that a row may be considered for explicit promotion.

It never means that live policy is automatically enabled.
