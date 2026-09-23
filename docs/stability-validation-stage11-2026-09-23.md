# Stage 11 — Session holdout / stability validation

## Goal

Stage 10 can show that a feature or economic threshold looks useful on a pooled multi-session dataset. Stage 11 asks a harder question:

> Does the effect survive when sessions are treated as independent holdouts?

This stage remains offline research only. No feature or threshold is automatically promoted into live trading logic.

## Validation unit

The holdout unit is an entire recorded session, not an individual trade.

That matters because trades inside one session are correlated by:

- market regime;
- volatility;
- symbol mix;
- data quality;
- execution conditions;
- strategy state progression.

Randomly splitting individual trades would leak session-specific structure between train and test.

## Feature stability

For each exact:

`strategy × side × LocalRegime × feature × value`

Stage 11 compares the selected feature value against all other values of the same feature inside the same strategy/side/regime group.

Current feature dimensions:

- multi-horizon flow alignment;
- EntryFreshness class;
- liquidity alignment;
- confluence count.

Example:

`trend_structure × LONG × bullish_trend × flowAlignment=short_term_reversal`

is compared against other flow-alignment values for that same playbook/side/regime.

### Per-session effect

For every session with enough selected and comparator samples:

`deltaAllInR = expectancy(selected) - expectancy(comparator)`

This gives the sign of the effect inside that session instead of relying on one pooled average.

### Leave-one-session-out

For each eligible holdout session:

1. all other sessions form the training set;
2. the feature effect sign is estimated on training sessions;
3. the same selected-vs-comparator effect is measured on the held-out session;
4. train/holdout sign agreement is recorded.

Feature validation statuses:

- `stable_positive`;
- `stable_negative`;
- `weak_effect`;
- `session_concentrated`;
- `unstable`;
- `insufficient_sessions`.

A candidate is additionally rejected as `session_concentrated` when more than 60% of its selected samples come from one session.

Even `stable_positive` / `stable_negative` rows remain `livePolicyEligible=false`.

## Fixed economic threshold stability

Every configured planned net-R:R threshold is also tested session-by-session.

For each exact strategy × side × LocalRegime group:

`effect = expectancy(RR >= threshold) - expectancy(RR < threshold)`

The same comparison is repeated per session and with leave-one-session-out folds.

This directly validates statements such as:

`RR >= 1.15 helps trend LONG × bullish_trend across sessions`

instead of:

`RR >= 1.15 happened to improve the pooled sample`.

## Threshold-selection holdout

Stage 11 also validates the threshold-discovery process itself.

For every holdout session:

1. only the other sessions are used to choose the best threshold;
2. the chosen threshold is frozen;
3. its pass-vs-fail expectancy is measured on the unseen holdout session.

A `stable_positive_holdout` candidate requires:

- enough eligible holdout sessions;
- positive holdout effect in the required fraction of folds;
- median holdout effect above the minimum effect size;
- reasonable consistency of the selected threshold across folds.

Threshold-selection consistency prevents a result from being considered stable when each training fold discovers a completely different R:R cutoff.

All threshold-selection results remain `livePolicyEligible=false`.

## Market-interaction stability

Market interaction checkpoints are grouped by:

`strategy × state × side × forward horizon × movement band`

Within each session:

`success rate = hypothesis_first / (hypothesis_first + opposite_first)`

Unresolved events do not decide the direction.

The report distinguishes:

- `stable_hypothesis_first`;
- `stable_opposite_first`;
- `unstable`;
- `insufficient_sessions`.

This is useful for testing whether states such as `continuation`, `break`, or `reject` are actually directional across multiple runs.

## Hindsight coverage stability

Coverage is also measured per session for:

`strategy × opportunity side × oracle-entry LocalRegime`

with dispersion of:

- observed coverage;
- tradeable coverage;
- actual trade coverage.

This prevents a high pooled coverage rate from hiding sessions where the playbook becomes effectively blind.

## Default validation policy

Defaults:

- minimum comparable sessions: 4;
- minimum selected/comparator samples per holdout session: 2;
- minimum selected/comparator samples in training folds: 6;
- neutral effect band: ±0.05 all-in R;
- minimum meaningful effect: 0.10 all-in R;
- required sign agreement: 75%;
- threshold-selection consistency: 50%;
- minimum resolved interaction checkpoints per session: 2;
- minimum hindsight opportunities per session: 2.

These are research readiness rules, not trading gates.

## Dataset output

`research-dataset-*.zip` now additionally contains:

`stability-validation.json`

The same validation is embedded under:

`cross-session-report.json -> stabilityValidation`

## Builder

Default:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-research-dataset.py
```

Validation settings can be tightened, for example:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-research-dataset.py --min-validation-sessions 6 --min-validation-session-samples 3 --validation-sign-agreement-rate 0.80
```

## Promotion rule

Stage 11 does not itself alter live policy.

A future trading rule should require at minimum:

1. causal live feature;
2. meaningful pooled effect;
3. enough independent sessions;
4. consistent per-session sign;
5. leave-one-session-out confirmation;
6. no single-session concentration;
7. for learned economic thresholds, stable threshold selection across folds.

Only after those conditions should a separate change explicitly promote the research candidate into an opt-in live gate.
