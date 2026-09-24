# Stage 27 — 10-hour research run

This profile is intended for a single controlled 10-hour paper session on the current main trading semantics. It changes recording policy and run duration only; it does not increase risk, leverage, or loosen strategy admission.

## Start

Update the repository and environment first:

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Optional but recommended for live latency dashboards:

```powershell
docker compose -f .\observability\docker-compose.yml up -d
```

Start the bot:

```powershell
.\scripts\run-research-10h.ps1
```

Open the UI and press Start. The paper run auto-stops after 36,000 seconds (10 hours), closes remaining paper positions, cancels pending entries, and writes `run_summary`.

The server stays open after the trading timer ends. Stop the server normally after confirming the run summary is present.

## Recording policy

The long-run profile records:

- research frames every 3 seconds;
- market/replay frames every 5 seconds while engaged and every 20 seconds while idle;
- public-trade tape in `research_frame` as `delta_v1`, using a monotonic trade sequence cursor;
- no duplicate trade tape in the replay/UI frame stream;
- semantic decisions, FIRE events, risk blocks, fills, exits, and latency traces unchanged.

This preserves the causal tape needed for offline reconstruction without repeatedly embedding the same rolling trade list in every frame.

If a delta cursor gap is detected after an abnormal stall/prune, the frame explicitly records `tradeDeltaGap=true` rather than silently pretending the tape is complete.

## Build the post-run bundle

After the server has stopped:

```powershell
.\scripts\build-latest-10h-pack.ps1
```

The command creates a directory next to the raw session:

```text
session-...-analysis-bundle/
├── bundle-manifest.json
├── session-...-overview.zip
├── session-...-hour-00.zip
├── session-...-hour-01.zip
├── ...
└── session-...-hour-09.zip
```

The raw `session-*.jsonl` is not copied into the bundle. Keep it locally as the lossless source of truth.

## Analysis workflow

Upload only `*-overview.zip` first.

It contains:

- `session-report.json` — global PnL, strategy/regime performance, missed-opportunity and market-interaction analysis;
- `latency-summary.json` — trade/FIRE latency traces plus the final Prometheus histogram snapshot with p50/p95/p99 approximations;
- `critical-events.jsonl` — trades, pending/cancel events, risk/arbiter blocks, strategy errors and run summary;
- `overview-analysis.jsonl` — sampled cross-market context for the whole run;
- `shard-index.json` — exact timestamps, sizes and archive names for each hour.

The first pass should identify:

1. profitable/unprofitable strategy × side × regime groups;
2. commission/slippage vs gross edge;
3. wrong-direction / late-entry / missed-opportunity clusters;
4. p50/p95/p99 bottlenecks in exchange → receive → parse → features → strategy → FIRE → order;
5. hours containing abnormal queue lag, recorder gaps, strategy errors, bad fills, stop clusters, or unusually strong missed moves.

Then upload only the referenced `hour-XX.zip` archives.

Each hourly shard keeps:

- all non-frame semantic/execution events for that hour;
- 3-second compact market frames;
- the delta trade tape;
- fast and deep book snapshots;
- deeper DOM samples around trades, rejects, FIRE/state transitions and other focus windows.

This lets detailed analysis work hour-by-hour instead of loading one monolithic multi-gigabyte archive.

## Manual tuning of pack size

The default one-hour shards can be changed:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-long-run-pack.py --shard-minutes 30
```

For a smaller overview:

```powershell
.\.venv\Scripts\python.exe .\scripts\build-long-run-pack.py --overview-frame-seconds 15
```

Do not reduce shard frame cadence or remove delta tape before the first Stage 27 run; those are the primary sources for causal post-trade reconstruction.
