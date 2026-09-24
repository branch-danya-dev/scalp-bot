# Session preservation and long-run analysis

Long research runs have two independent outputs with different purposes.

## Lossless raw archive

The raw recorder JSONL is the source of truth and is never committed to Git.
For each session the preservation command creates:

    data/raw-archives/
      session-20260924TxxxxxxZ/
        session-20260924TxxxxxxZ.jsonl.zst
        session-20260924TxxxxxxZ.jsonl.zst.sha256
        metadata.json

metadata.json stores both the raw JSONL SHA-256 and the compressed archive
SHA-256, plus event counts, symbols, duration, run summary and optional
provenance. The original session-*.jsonl is never modified or deleted.

Optional second-copy mirror:

    .\scripts\archive-latest-session.ps1 -RawArchiveMirrorPath "H:\scalp-bot-raw-backup"

The copied archive is checksum-verified after the mirror write.

Do not stamp the current Git HEAD onto an older session. If the exact run
commit is known, pass it explicitly:

    .\scripts\archive-latest-session.ps1 -BotCommit "<exact-run-commit>" -RunProfile ".env.research-10h"

If the commit is unknown, leave it empty instead of recording false provenance.

## Streaming analysis export

The Git export is rebuildable analysis data. It now uses two streaming passes:

1. pass 1 discovers focus windows and compact global counters;
2. pass 2 writes bounded overview, indexes and time shards directly.

It does not build a temporary ZIP bundle or a monolithic session-report.json.
Latency details are also stored in bounded JSONL parts.

Native delta_v1 trade prints are accumulated across downsampled research
frames before a compact frame is written. Legacy rolling_v1 sessions are
deduplicated by trade sequence.

## Recommended post-run command

Normal use:

    .\scripts\finalize-latest-session.ps1 -RunProfile ".env.research-10h"

With a second raw copy:

    .\scripts\finalize-latest-session.ps1 -RunProfile ".env.research-10h" -RawArchiveMirrorPath "H:\scalp-bot-raw-backup"

Finalization preserves the raw archive first. Git export/push starts only after
the archive succeeds, so a GitHub failure cannot invalidate the source run.

## Retention

Keep the immutable .jsonl.zst archive, checksum and metadata permanently.
Keep important raw archives in at least two storage locations. The Git runs
repository is an analysis/index layer and can always be rebuilt from raw data.
