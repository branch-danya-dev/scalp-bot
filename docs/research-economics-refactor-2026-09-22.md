# Research economics refactor — 2026-09-22

This note summarizes the refactor triggered by the paper run in `paper-analysis-20260922-203956.zip`.

## What the run showed

The run lasted roughly 40 minutes and opened no positions even though the strategy engine produced valid tradeable breakout decisions.

The important distinction was:

- strategy detection was not completely inactive;
- several breakout opportunities reached LONG/SHORT;
- economic/risk policy then rejected every tradeable plan.

### NEAR regression pair

The archive now lives as a small regression fixture in:

```
tests/fixtures/paper_20260922_regressions.json
```

Two NEAR cases are deliberately preserved:

1. **17:06:57 UTC**
   - setup quality ~0.903;
   - pressure score 4;
   - 5s flow imbalance +1.0;
   - old nearest target 4.465;
   - old target was reached ~39 seconds later;
   - old stop was not reached in the 3-minute review window.

2. **17:10:40 UTC**
   - setup quality ~0.766;
   - pressure score 3;
   - 5s flow imbalance ~+0.718;
   - old target was not reached;
   - old stop would have been reached ~149 seconds later.

The refactor does **not** hard-code these quality numbers as an entry threshold. They are preserved so future policy changes do not erase the good/bad distinction observed in the archive.

### Density review

The archive contained 10 old states labelled `DEFENDED`.

Inspection showed that none had completed both:

```
price reaction away from wall
+
fresh local flow reversal in the same direction
```

Every one subsequently transitioned to `EXHAUSTED`.

The old state name was misleading. Pre-entry touch/hold is now `TEST`; `DEFENDED` is reserved for actual price+flow confirmation.

## Changes implemented

### 1. Research economic gates are shadow diagnostics

The following legacy requirements no longer block research paper entries:

```
minimum net >= max($1, 0.1% equity)
net reward / all-in loss >= 1.15
```

They remain calculated and recorded as:

```
economic_shadow
```

with explicit reasons.

A trade still cannot open if its configured target is non-positive after estimated costs.

Strict mode can re-enable both gates independently.

### 2. Structural risk is separated from transaction costs

`SCALP_RISK_FRACTION=0.005` now means structural price risk to the strategy invalidation point.

Research defaults:

```
structural risk budget          0.50% equity
per-trade planned all-in cap    1.25% equity
aggregate open all-in cap       2.00% equity
single-position leverage cap    5x
portfolio leverage cap          10x
```

Fees/slippage no longer silently reduce the intended structural risk budget. They are controlled by the separate all-in caps.

### 3. Breakout geometry

After confirmed breakout acceptance:

- emergency stop is based on re-acceptance into the broken zone rather than automatically hiding behind the whole zone;
- nearby liquidity is `nearestObstacle`, not automatically final target;
- liquidity is treated as an ordered ladder;
- a final liquidity target must meet the breakout minimum structural target distance;
- otherwise the strategy uses an impulse/risk fallback.

This directly protects the NEAR failure mode where a very near previous-day high collapsed reward while the stop remained far away.

### 4. Trend / rejection / density target geometry

These strategies already intended a 1.6R reaction/continuation target.

Previously, `find_liquidity_target()` could silently replace that with a much closer level.

Now:

- closest liquidity is recorded as `nearestObstacle`;
- a structural liquidity target is used only when it is at least as far as the strategy's existing 1.6R target geometry;
- otherwise the original 1.6R risk-multiple target remains final.

No new target-R parameter was invented for these strategies.

### 5. Execution costs are asymmetric

Entry confirmation is still modeled conservatively as taker market execution.

Target exit is modeled as a resting maker limit.

Stop, structural invalidation, manual/session close and partial exits remain taker-market.

Therefore target-path and stop-path transaction costs are no longer assumed identical.

Important: passive maker **entry** is not credited yet. Rejection/density are marked as passive-entry-eligible in the execution profile, but paper trading continues to charge taker entry until an actual pending PostOnly fill model exists.

### 6. Full reject diagnostics

Economic rejects now return their complete calculated snapshot:

- entry / stop / target;
- structural risk budget;
- per-trade all-in cap;
- structural and all-in sizing caps;
- effective leverage;
- spread and depth impact;
- target-path costs;
- stop-path costs;
- gross target;
- structural stop loss;
- net target;
- all-in stop loss;
- net R:R;
- enabled/disabled gates.

Earlier rejects still contain the available market/portfolio/entry-stop-target context.

The snapshot is stored in the `risk_reject` event and surfaced in Observatory.

### 7. Archive regressions

Regression tests preserve:

- NEAR good breakout;
- NEAR weak breakout;
- all 10 density cases previously labelled DEFENDED.

The purpose is not to optimize the algorithm to one session. The fixture protects known failure semantics while allowing future changes to be tested against real recorded cases.

### 8. Per-strategy expectancy framework

A strategy no longer needs to inherit one universal RR assumption forever.

Each strategy independently tracks:

- trades;
- wins/losses;
- win rate;
- average win USD;
- average loss USD;
- net PnL;
- average realized net R (expectancy R).

Default policy:

```
minimum sample size: 30 closed trades
expectancy gate: OFF
minimum expectancy R: independently configurable per strategy
```

Until the sample is large enough, status is `insufficient_samples`.

The research bot does not block a strategy from a handful of outcomes.

## What was intentionally NOT changed

- 5x single-position exposure cap;
- 10x portfolio exposure cap;
- 2% aggregate open all-in risk cap;
- trade-flow imbalance thresholds;
- density consumption/depletion limits;
- 1R partial trigger;
- 70/30 partial split;
- no-follow-through timer;
- early-cut thresholds;
- runner fallback;
- hard pressure-score threshold for breakout.

In particular, the archive's stronger NEAR setup had pressure 4 while the later bad setup had pressure 3. That difference is recorded, but it was **not** promoted into a new hard filter from a sample of two.

## Current research philosophy

The next research observation should answer:

1. which strategies actually trade after removing the artificial universal economics blockade;
2. how many trades violate the old $1 / 1.15R rules but still produce positive expectancy;
3. actual fee share by strategy and outcome;
4. whether maker target execution materially changes economics;
5. whether nearest obstacles predict partials/failures;
6. whether strategy-specific realized expectancy stabilizes with enough samples.

Only after sufficient samples should the strategy expectancy gate be enabled.
