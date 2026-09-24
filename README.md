# Scalp Bot — Stage 19

Paper-trading research bot for short-horizon crypto perpetual strategies.

The current main branch is the post-Stage-17 technical/economic remediation
state. It is **not** approved for a 12-hour audit until the Stage 19 code audit
and short smoke are explicitly completed.

See:

- `docs/stage27-10h-run.md` — current controlled 10-hour run and sharded post-run analysis workflow.
- `docs/stage19-technical-economic-remediation.md` — earlier remediation plan
  and validation order.
- `docs/trade-observatory.md` — replay/decision observability.
- `docs/research-economics-refactor-2026-09-22.md` — earlier economics
  investigation.

## Current trading policy

Tradeable playbooks:

- `weak_level_rejection`: early failed-break + local absorption.
- `level_breakout`: mature horizontal level, ARMED pressure, accepted break,
  absorption veto and either retest+hold or sustained hold with real
  directional price response.

Non-tradeable research components:

- `trend_structure`: disabled until continuation logic proves forward edge.
- `orderbook_density`: liquidity evidence only; it does not independently
  open positions.

Breakout/rejection staged adds remain implemented for research but are disabled
in the current policy.

## Market model

The engine combines:

- confirmed 1m / 5m / 15m / 1h candles;
- the forming 1m candle (body, range, wick position, range expansion, volume
  pace and velocity);
- public-trade flow/CVD and 5s/15s/60s imbalance;
- best-level OFI and order-book liquidity evidence;
- shared structural levels/trendlines;
- current and previous UTC-day highs/lows;
- a LocalRegime/HTF context layer;
- exact ARMED/FIRE causal telemetry.

LocalRegime is context/preference for breakout and rejection, not a hard side
selector.

## Structural correctness

- a selected breakout zone stays bound to the exact StructuralLevel generation;
- distinct level approaches require a real departure plus time/bar separation;
- ordinary and daily reference levels share one support/resistance taxonomy;
- day/previous-day extremes are structural obstacles by definition;
- hard breakout stops sit beyond the complete broken zone; reacceptance inside
  the zone is a separate soft invalidation.

## Execution model

- entries and emergency/invalidated exits use executable book depth;
- taker entry geometry includes configured slippage in the expected fill;
- maker entries, partials and targets require public-trade-through confirmation;
- pending maker entries are cancelled immediately if their setup becomes
  invalid, late or changes identity;
- spread is represented by executable bid/ask and is not subtracted again.

## Economics and risk

Default research account: $1,000.

- base structural risk: 0.5% equity per setup;
- max planned all-in loss: 1.25% equity per position;
- aggregate open all-in risk cap: 2% equity;
- max gross single-position exposure: 5x equity;
- max gross portfolio exposure: 10x equity;
- absolute planned net reward/risk floor: 1.0;
- positive risk scaling above base is disabled until expectancy proves edge.

RiskEngine and PaperBroker use the same partial/runner assumptions. A partial is
planned only when that leg can meet the same economic threshold used by the
paper broker. Weak-level rejection currently closes 30% at the early partial;
breakout requires at least 2R gross target room before costs.

## Scanner

The active-symbol scanner separates raw activity from opportunity readiness.
Readiness rewards compression -> fresh expansion while penalizing already-spent
moves, reducing the old tendency to select only coins whose impulse had already
occurred.

## Telemetry

Minor `market_context_changed` churn is compactly sampled (10s default);
semantic regime/structure changes emit immediately. Full causal snapshots
remain attached to decisions, state transitions, risk events and trades.

This makes a future 12-hour session tractable without removing the data needed
for strategy analysis.

## Local setup

```powershell
git fetch origin
git switch main
git pull --ff-only origin main

.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The normal launcher loads `.env.example` first, then an optional checked-in
research profile, so a stale local `.env` cannot silently alter a controlled
run.

Prepared but **not yet authorized**:

```powershell
.\scripts\run-stage19-audit-smoke.ps1
```

The exact 12-hour launcher remains:

```powershell
.\scripts\run-global-audit-12h.ps1
```

Do not launch the 12-hour profile before the Stage 19 smoke is reviewed.
