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
- confirmed entry is modeled as taker; resting final target as maker-limit; stop/invalidation/partial as taker;
- the old $1 minimum-net and 1.15 net R:R rules are recorded as shadow diagnostics rather than hard research gates;
- target geometry records nearest liquidity as an obstacle rather than automatically shrinking final targets;
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


## Post-run report artifact

After the run has stopped and `run_summary` exists, build a compact analysis report from the recorded session:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-session-report.py
```

The command selects the latest `data/sessions/session-*.jsonl` and writes a sibling `session-*-report.json` containing:

- `runSummary`;
- full Post-run Opportunity Review output;
- closed-trade records and compact Trade Review data;
- scanner/universe history and latest ranked coins;
- per-symbol chart candles and data-coverage metadata;
- explicit confirmation that raw order-book/research/replay frames remain in the source `session-*.jsonl`.

The report deliberately does not duplicate every raw order-book frame. Keep the report JSON and its source JSONL together in the analysis archive: the report is the compact index/derived layer, while the JSONL remains the lossless market-data source for deep book and replay analysis.


## Compact analysis pack

Full research sessions can exceed 1 GB because 1-second research frames may contain deep order books. Keep the original `session-*.jsonl` locally as the lossless source of truth and generate a separate upload artifact for model review:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-analysis-pack.py
```

The command selects the latest session and writes `session-*-analysis-pack.zip`.

The pack contains:

- all non-frame events (decisions, rejects, entries, partials, exits, scanner updates, run summary);
- 1-second compact market frames with candles, flow, position state and top-5 book;
- the complete derived post-run report, including Opportunity Review and Trade Review;
- periodic top-16 order-book samples;
- deeper top-50 order-book samples around entries, exits, rejects and other focus events.

This is the artifact intended for ChatGPT/Claude. Do not upload the multi-gigabyte raw session unless a specific lossless forensic check is required.


## Fee-execution v6

The next research profile is `research-fee-execution-v6-1h`.

Compared with the previous run it intentionally changes execution economics rather than weakening signal confirmation:

- expected winner economics price the actual partial + runner lifecycle;
- winner transaction costs may not exceed 35% of lifecycle gross profit;
- structural stops remain strategy invalidation points and are not widened to hide fees;
- breakout takes 30% at 1R and keeps 70% as runner;
- density/rejection retain 70% partials; trend uses 50%;
- profit partials are resting maker limits and require trade-through confirmation;
- density/rejection prefer a conservative PostOnly maker entry with a 15s timeout;
- pending maker entries reserve portfolio risk and exposure;
- breakout no-follow timeout is 120s; density remains 20s;
- working/active universe expands from 4/8 to 6/12 symbols.

The purpose of the run is to measure whether fee share falls materially while opportunity throughput remains usable. Passive fills, timeouts and cancelled entries are recorded explicitly.


## Frequency v7

The next research profile is `research-fee-frequency-v7-1h`.

The previous 52-minute run showed that rare trading was not caused by the risk engine: it recorded zero risk rejects and zero setup blocks. The remaining bottleneck was strategy confirmation.

Targeted changes:

- `trend_structure`: the old "micro reclaim" was the maximum high/minimum low of the previous three closed 1m candles. In recorded XRP tests that put the reclaim threshold roughly 69-81 bps away from the trendline. Reclaim is now local to the tested trendline using spread + existing test tolerance; flow confirmation and continuation are still mandatory.
- `weak_level_rejection`: once a level is actually tested, the setup is pinned for up to 150 seconds. Sweep state is remembered and a later live reclaim plus local flow reversal can confirm the trade instead of requiring the sweep and full reclaim to coexist in one closed 1m candle.
- `level_breakout`: pressure/acceptance/hold gates remain unchanged because the prior run showed they filtered both a false XRP break and an early SOL break.
- `orderbook_density`: reaction confirmation remains unchanged because most unconfirmed wall tests produced moves too small relative to costs.

These changes are intended to increase completion of already-valid setup sequences, not to turn early APPROACH/FOUND states into trades.
