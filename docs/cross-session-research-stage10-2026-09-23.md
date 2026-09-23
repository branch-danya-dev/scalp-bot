# Stage 10 — Cross-session research dataset

## Goal

Move research from single-session anecdotes to a cumulative dataset with explicit session provenance.

Stage 10 does not change trading behavior. It is an offline aggregation pipeline over already-recorded sessions.

## Supported sources

The dataset builder accepts:

- raw `session-*.jsonl` files;
- standalone `*-report.json` session reports;
- `*-analysis-pack.zip` archives.

When the same session is supplied more than once, it is deduplicated by a canonical session identity derived from source session filename, run label, run window and event count.

If multiple representations of the same session exist, the richest source wins:

`raw session > analysis-pack > standalone report`

This prevents a lightweight report from replacing a source that still contains event-level arbiter diagnostics.

## Source isolation and leakage

Each session is analyzed independently before aggregation.

Stage 10 does not concatenate raw market streams and rerun hindsight across session boundaries.

This is important because:

- oracle entry/exit labels are future-informed within their source session;
- LocalRegime/flow/liquidity/EntryFreshness must remain causal features from that session;
- no feature from a later session may influence an earlier session.

The cross-session layer only aggregates already-produced per-session labels and causal features.

## Normalized tables

The generated ZIP contains:

- `sessions.jsonl` — provenance and run metadata;
- `trades.jsonl` — normalized closed trades with planning-time context/economics;
- `hindsight-opportunities.jsonl` — independent market opportunities and strategy-fit labels;
- `market-interactions.jsonl` — causal interaction checkpoints;
- `arbiter-blocks.jsonl` — semantic veto events when event rows are available;
- `cross-session-report.json` — cumulative metrics/hypothesis tables;
- `manifest.json` and `README.txt`.

Every normalized record receives a stable dataset id plus:

- `sessionId`;
- `runLabel`;
- source filename.

## Cross-session hypotheses

### Actual trade feature outcomes

`tradeFeatureOutcomes` groups actual closed trades by:

`strategy × side × LocalRegime × feature dimension × feature value`

Current dimensions:

- multi-horizon flow alignment;
- EntryFreshness class;
- liquidity alignment;
- confluence count.

Each row reports:

- samples;
- number of contributing sessions;
- wins/losses/win rate;
- gross/fees/net;
- net per trade;
- expectancy in realized all-in R;
- MFE/MAE.

This directly supports questions such as whether `short_term_reversal` is consistently harmful across multiple sessions rather than one run.

### Hindsight coverage

`hindsightCoverage` recomputes strategy coverage across all sessions for:

`strategy × opportunity side × oracle-entry LocalRegime`

with:

- opportunities;
- sessions;
- observed;
- tradeable;
- traded;
- observed/tradeable/trade coverage rates;
- strategy-fit counts;
- bot-comparison counts.

`orderbook_density` is excluded because it is an evidence provider, not a tradeable playbook.

### Market interaction outcomes

`marketInteractionOutcomes` aggregates every recorded horizon/band classification across sessions while preserving:

- strategy;
- state;
- hypothesis side;
- forward horizon;
- move band;
- classification;
- sample count;
- session count.

### Arbiter diagnostics

`arbiterBlockSummary` aggregates semantic veto reasons by strategy.

Arbiter aggregation has an explicit coverage caveat: standalone reports do not contain individual `arbiter_blocked` events. The report therefore includes `sessionsWithEventRows` / `sessionsWithoutEventRows` and an event-coverage warning.

### Economics

All normalized trades are passed through the Stage 9 conditional economic calibration again at cross-session scope.

This is the first layer where exact `strategy × side × LocalRegime` groups can realistically reach readiness thresholds without forcing a single run to contain dozens of trades.

## Builder

Default usage over all raw sessions in `SCALP_SESSION_DIR`:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-research-dataset.py
```

Recent N sessions only:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-research-dataset.py --last 10
```

Explicit sources or globs:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-research-dataset.py "data\sessions\session-*.jsonl"
```

The default output is:

`data/sessions/research-dataset-YYYYMMDDTHHMMSSZ.zip`

## Interpretation rule

A feature or threshold should not become a live rule merely because it looks good in this aggregate.

Required next step is stability validation across sessions/regimes: the sign and magnitude of the effect should persist when sessions are held out rather than pooled into the same discovery sample.

That becomes the next research layer after Stage 10.
