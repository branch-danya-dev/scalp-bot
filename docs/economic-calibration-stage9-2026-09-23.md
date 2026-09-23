# Stage 9 — Conditional economic calibration

## Goal

Replace universal economic assumptions with measured conditional evidence while keeping trading behavior unchanged until enough out-of-sample data exists.

The calibration key is:

`strategy × side × LocalRegime`

Examples:

- trend_structure × LONG × bullish_trend
- trend_structure × SHORT × bearish_trend
- level_breakout × LONG × range

## Why planned net R:R needs conditional calibration

A planned net reward/risk value describes the configured payoff geometry if the plan reaches its target/stop lifecycle. It does not describe the probability of either outcome.

A universal rule such as `net R:R >= 1.15` can therefore help one playbook/side/regime and hurt another.

Stage 9 measures that explicitly.

## Comparable realized R

Stage 8 kept `realizedR = net PnL / structural initial risk` for trade-performance diagnostics.

Stage 9 additionally calculates:

`realizedAllInR = net PnL / planned all-in loss USD`

This denominator matches the all-in stop-side loss used by RiskEngine when it calculates planned net reward/risk, making planned and realized economics directly comparable.

## Per-group calibration

For each exact strategy × side × LocalRegime group the report includes:

- baseline samples / win rate / gross / fees / net;
- baseline expectancy in realized all-in R;
- average/median planned net R:R;
- planned breakeven win rate and actual-minus-breakeven win rate;
- planned net-at-target and all-in-loss amounts;
- winner/stop cost share;
- first-take movement.

### Net-R:R bands

The default descriptive bands are:

- <0.50
- 0.50–0.75
- 0.75–1.00
- 1.00–1.15
- 1.15–1.25
- 1.25–1.50
- 1.50–2.00
- >=2.00

Each band reports the actual outcome expectancy of trades that were planned inside that band.

### Threshold sweep

The report also evaluates retained historical trades under thresholds:

`0.50, 0.75, 1.00, 1.15, 1.25, 1.50, 2.00`

For each threshold it records:

- retained sample count;
- coverage rate;
- actual expectancyAllInR;
- net PnL;
- win rate;
- delta expectancy versus the unconditional baseline.

The same idea is applied to winner-cost-share thresholds.

## Universal 1.15R counterfactual

Every research-ready group receives a descriptive verdict for the historical universal 1.15R gate:

- `improves_observed_expectancy`;
- `reduces_observed_expectancy`;
- `roughly_neutral`;
- `insufficient`.

This verdict is group-specific. It is not promoted to a global trading rule.

## Recorded gate counterfactuals

The report separately compares pass/fail historical outcomes for the gate flags recorded at planning time:

- minimum net profit;
- net reward/risk;
- first-take movement.

This directly answers questions such as whether trades that would have failed the old economic gate were actually worse in that conditional group.

## Readiness guards

Default research readiness:

- exact group: 20 trades with all-in R;
- threshold segment: 8 retained trades with all-in R.

These are configurable as:

- `SCALP_ECONOMIC_CALIBRATION_MIN_GROUP_SAMPLES`;
- `SCALP_ECONOMIC_CALIBRATION_MIN_SEGMENT_SAMPLES`.

They are recorded in `bot_started.config`, so post-analysis uses the policy of the actual run.

Readiness affects interpretation only. It does not reject trades.

## Best observed threshold is not a policy

When a group is research-ready, Stage 9 exposes `bestObservedRrThreshold` from the same historical sample.

It is intentionally marked:

- `inSampleOnly=true`;
- `policyEligible=false`.

Selecting a threshold and evaluating it on the same trades is optimistic and can overfit. A candidate threshold must be validated on later sessions before becoming an enforcement rule.

## Hierarchical references

Exact groups remain the primary calibration key.

For sparse data the report also exposes broader reference scopes:

- strategy × side;
- strategy.

These references are diagnostic context only; they are not substituted for the exact group when making a policy claim.

## Trading behavior

Stage 9 does not add a conditional hard gate.

Existing RiskEngine behavior is unchanged. Current strict/shadow settings continue to determine whether the legacy economic gates are enforced.

## Output

Session report and Opportunity Review expose:

`conditionalEconomicCalibration`

with:

- policy/readiness metadata;
- summary;
- exact groups;
- broader reference groups.

## Next step

Collect new paper-run data under the Stage 6–9 architecture. Only after enough exact groups become research-ready should candidate thresholds be validated on subsequent runs and considered for an opt-in conditional policy.
