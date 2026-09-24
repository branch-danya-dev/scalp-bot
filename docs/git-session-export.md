# Git session export for long research runs

The raw `session-*.jsonl` file remains the local lossless source of truth.
It is intentionally **not** committed to GitHub.

For ChatGPT/Claude analysis, the bot creates a smaller text-only analysis tree
that is safe to browse through GitHub and scales to 10-20+ hour runs.

## Layout

```text
runs/
  session-20260924TxxxxxxZ/
    manifest.json
    README.md
    overview/
      session-report.json
      latency-summary.json
      critical-events/
        part-0000.jsonl
      frames/
        part-0000.jsonl
    index/
      shards.json
      trade-events/
        part-0000.jsonl
      problem-events/
        part-0000.jsonl
    shards/
      0000-0030/
        manifest.json
        analysis/
          part-0000.jsonl
        orderbooks/
          part-0000.jsonl
      0030-0060/
        ...
```

Default policy:

- time shard: 30 minutes;
- each JSONL file part: at most 20 MB;
- overview frames are sampled;
- deep order-book data is retained at higher resolution around important events;
- native delta trade tape is preserved;
- legacy `rolling_v1` trade tape is converted to delta during export;
- raw session JSONL is never copied into the runs repository.

A 20-hour run therefore has about 40 time directories by default. Heavy shards
may contain multiple 20 MB parts, but no individual file grows with total run
duration.

## Analysis workflow

Start with:

1. `manifest.json`;
2. `overview/session-report.json`;
3. `overview/latency-summary.json`;
4. `index/shards.json`;
5. compact `index/trade-events/*` and `index/problem-events/*`.

Only after an interesting interval is identified should the corresponding
`shards/HHHH-HHHH/analysis/*` and `orderbooks/*` files be opened.

This keeps long-run analysis selective instead of reading the entire run.

## One-time runs repository setup

Create a dedicated repository once:

```powershell
gh repo create branch-danya-dev/scalp-bot-runs --public --add-readme
```

The publishing script uses the sibling directory `..\scalp-bot-runs` by
default and will clone it automatically after the GitHub repository exists.

## Export and publish latest session

From the `scalp-bot` repository:

```powershell
.\scripts\export-and-publish-latest-session.ps1
```

This builds the analysis export from the latest local session, commits it to
`scalp-bot-runs/runs/<session>/`, and pushes `main`.

For an already-exported session:

```powershell
.\scripts\publish-session-export.ps1 -ExportPath .\data\git-exports\session-20260924TxxxxxxZ
```

For a local check without pushing:

```powershell
.\scripts\export-and-publish-latest-session.ps1 -NoPush
```

To rebuild an existing local export:

```powershell
.\scripts\export-and-publish-latest-session.ps1 -OverwriteExport
```

## Current oversized one-hour session

The exporter can be run against the existing session without repeating the
paper run. Its legacy rolling trade tape is deduplicated during export, so the
Git representation should be much smaller than the 310 MB analysis ZIP.

## Retention

The dedicated runs repository should contain analysis-grade exports only.
Do not add raw JSONL sessions, ZIP bundles, database files, screenshots, or
binary telemetry. Keep raw sessions locally for exact forensic reconstruction.
