# Local verification and CI receipt

Base: `f557e79a9735015b497e21d9c04eab058b09b72d`, draft PR58. Separate worktree/venv, Python 3.13.15 on Windows. No Demo credential profile, authenticated API call, new public market stream, order or measured hour was used.

| Check | Executed result | Scope / receipt |
|---|---|---|
| Full local preflight on `c726437` | 1442 passed, 244.11 s | `full-preflight-v2.txt` |
| Full local preflight on `cdbc0fd` | 1453 passed, 240.72 s | `full-preflight-v3.txt` |
| Full local preflight on `fa2df42` | 1455 passed, 247.95 s | `full-preflight-v4.txt` |
| Final audit + lifecycle/ML foundation/maker/transport on `ae8017e` | 137 passed, 6.16 s | `ml-audit-after.txt`; includes the four new V2 metadata/funnel cases |
| JavaScript syntax | both files passed | `node --check scalp_bot/static/app.js` and `replay.js` |
| CLI and Windows launcher local preflight | passed, connected=false, orders_sent=0 | `local-preflight-pinned.json`, `launcher-preflight-final.json` |
| Historical integration on `fa2df42` | 36,121 selected events, 36,053 market messages, seven symbols, 4,757 evaluations | `offline-final-v3/report.json`; selected event checksum below |
| Saved real V2 worker on `fa2df42` | 60 validation forecasts, 0 selected at .55, exchange_imported=false | same report plus `real-v2-forecasts.json`; no training/tuning |

The last source audit only pins the already-used calibration manifest and records previously omitted rejection reasons. The final complete Linux suite and Windows spawn/ML/Demo suite run in the existing GitHub Actions workflow on the pushed documentation head, including those last four regressions. The **exact head SHA, run URL, job conclusions, test totals and downloaded logs are retained in `G:/scalp-bot/data/audit-demo-paper-preparation/final-ci.json` and linked in [PR58](https://github.com/branch-danya-dev/scalp-bot/pull/58)**. A prior head's green result is not used as the final receipt. The receipt is separate to avoid a self-referential commit changing the head it attests to.

## Reproduced defects and controls

- Original full attempt: 1432 passed / 1 failed, the legacy blanket import assertion. Only the opt-in namespace was exempted; ordinary engine imports neither ML nor Demo research, checked in a separate process.
- Account-scope audit: 8 failed / 3 passed before repair; preflight now includes USDC and REST gap recovery inspects USDT, USDC, inverse, options and spot orders. Unknown nonzero private positions halt immediately. After repair, 49 Demo/transport tests passed.
- Report boundaries: 2 failures before repair. HTTP send is timestamped after the serialized REST queue/pacing wait; unconfirmed funding is blank, separately from observed cash. After repair, 133 selected regressions passed.
- V2 metadata/funnel: 4 failures before repair. A modified calibration manifest with unchanged weights is rejected; clock/inactive-symbol/instrument rejection records remain in the delivered-forecast funnel. After repair, 137 selected regressions passed. An earlier fixture-only missing-symbol error and its corrected reproducer both remain in evidence.
- Positive and negative maker receipt controls, partial fills, deduplication, uncertain create lookup, Stop/reduceOnly, shared risk/queue pressure, worker loss/abstention, environment guards and signal handling remain covered. Whole Session lifecycle is tested with all network boundaries replaced by mocks; temporary synthetic reports are not a measured market hour.

## Immutable inputs and limits

Selected original-event SHA256: `b99f2eb5bebf254a5cd1374d8face78356cfc5e2c440373154e1bf29b15e1f77`. Repeated bounded history selected the same input bytes and counts. Original recording was opened read-only; captured controls cannot authorize a network run. Historical integration produced no positions and is not an execution/PnL comparison or latency certification. Local functional tests may run concurrently with replay; no new isolated latency result is claimed.

V2 weights: `a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2`. V2 manifest: `15b55fa2c360729cebb37d8cb9cbe432249907873d7016b3b81929df173082f3`. Both identities are enforced by preflight. Original main checkout remains `3403b03011a5e89f6f0c4ecb03c6f76df4b7f1e1`; existing untracked `data/pr58-completion/` is preserved. No diff from the base task touches old reports, datasets, V1/V2 or PR59's protocol.

Large logs/raw predictions stay under the local root above. [Evidence manifest](evidence-manifest.json) records the completed preparation artifacts; `final-ci.json` adds its own final-head log checksums after CI completion. Failed attempts are retained, not overwritten. [Commands](COMMANDS.md), [protocol](../../DEMO_PAPER_1H_PROTOCOL.md), [implementation review](../../DEMO_PAPER_IMPLEMENTATION_REVIEW.md).
