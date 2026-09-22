# One-hour diagnostic research run

Purpose: validate the revised trading methodology on fresh market data before any longer paper campaign.

This run is **not** for tuning thresholds during the session. It uses the current `main` strategy/risk/execution configuration unchanged and only shortens the automatic paper duration to one hour.

## Profile

```text
SCALP_RUN_LABEL=research-economics-v5-1h
SCALP_PAPER_RUN_DURATION_SECONDS=3600
```

Everything else is inherited from `.env.example`, including:

- trend-only entries;
- confirmed/causal candle context;
- local tape confirmation and relative participation;
- real sweep/reclaim semantics;
- breakout acceptance;
- depth-aware entry VWAP;
- 0.5% structural price risk to the strategy invalidation point;
- 1.25% maximum planned all-in loss per trade including fee/slippage reserve;
- target must remain net-positive after estimated costs;
- the old $1 minimum-net and 1.15 net R:R rules are recorded as shadow diagnostics rather than hard research gates;
- adaptive full-depth recording for engaged density setups;
- partial/runner and structural invalidation rules.

## What this hour is intended to answer

1. Do the four strategies still produce enough observable opportunities after the methodology hardening?
2. Are rejected entries rejected for sensible reasons: trend/context, flow, economics, entry drift, spread/depth, or consumed structure?
3. For trades that open, did the recorded market state actually match the intended strategy sequence?
4. Did actual paper entry VWAP materially differ from top-of-book, and did depth filtering reject thin books?
5. Did structural invalidation/no-follow-through close trades for a defensible market reason rather than merely because PnL moved against the position?
6. Does setup quality/activity ranking choose the better simultaneous opportunity without overriding strategy eligibility?
7. Are density decisions replayable with the recorded deep book when a wall becomes engaged?

## What not to change during the hour

Do not tune:

- imbalance thresholds;
- partial trigger or 70/30 split;
- no-follow-through timing;
- MFE/MAE early-cut thresholds;
- runner fallback R;
- structural risk fraction;
- per-trade all-in loss cap;
- shadow minimum-net / net R:R thresholds.

Changing them inside this run would contaminate the observation.

## Start

From the repository root:

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
powershell -ExecutionPolicy Bypass -File .\scripts\run-research-1h.ps1
```

The launcher runs the complete preflight test suite first. When the server is ready, open the UI and press **Start**. The one-hour clock begins only then.

At 3600 seconds the engine will:

- stop accepting new entries;
- close remaining paper positions;
- record the resulting `trade_closed` events;
- write `run_summary`;
- keep the server/replay UI available for inspection.

## Acceptance for the run itself

The run is operationally valid only if:

- preflight tests pass;
- UI shows paper/research mode;
- run label is `research-economics-v5-1h`;
- configured duration is 3600 seconds;
- no `strategy_error`, unrecovered book desync, or persistent scanner/bootstrap failure invalidates the observation;
- a final `run_summary` exists.

A low trade count is a research result, not automatically a failure. The first analysis should inspect the signal/rejection funnel before relaxing any rule.
