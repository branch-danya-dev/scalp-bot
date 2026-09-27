# Demo / paper implementation review

Base: PR58 `f557e79a9735015b497e21d9c04eab058b09b72d`. Work remains in the separate `parallel-scenarios-ml` worktree/venv on `codex/parallel-scenarios-ml-v1`. [Protocol and owner commands](DEMO_PAPER_1H_PROTOCOL.md); [machine passport](docs/demo-paper-1h/passport.json); [source task](docs/demo-paper-1h/TASK.md).

## Implemented

Default-off restricted Demo REST/private WS, explicit order state, uncertain-create lookup, WS/REST deduplication, partial/late fills, cancel/amend reconciliation, reduce-only closure and independent paper matching. One existing scanner/context/ordinary arbiter, atomic paired reservation, bounded entry queues, per-arm management, ownership through both terminal reconciliations. No broad cancel-all or private mainnet client.

The existing production path is unchanged except opt-in extension points and extraction of the same runner calculation for reuse. PR58's pre-entry maker receipt guard remains. Old raw, reports, datasets and V1/V2 weights are untouched; no new trading rule, leverage, stop, partial fraction or quote_tape default was introduced. No PR59 change.

Research-only V2 admission is separate from shadow. Original grid, schema, weights, .55 and 30/15/30 plan remain. Worker receives no exchange credentials or broker reference. Parent retains position protection if prediction stops. Actual stage records and fee-normalized comparison are produced only for a separately started experiment.

## Executed local evidence

- Contract/integration suite includes safe hosts/redirects/no-order preflight, uncertain POST, duplicate/late/partial fills, maker creation/amendment receipt fences, both ML sides/abstention/staleness/schema/drift, shared risk/queue pressure, transport invalidation, worker environment, OS signals and a complete mock Start→partial→Stop→REST reconciliation/report path.
- Bounded historical integration used 36,121 events (36,053 market messages), seven symbols and 4,757 actual engine evaluations over 179.993 seconds of the original stopped recording. No network or portfolio claim. Source selection checksum `b99f2eb5bebf254a5cd1374d8face78356cfc5e2c440373154e1bf29b15e1f77`.
- Real saved V2: 60 pre-existing validation snapshots, 60 forecasts, zero selected at .55. No training or tuning. First diagnostic harness imported engine code during Windows spawn; that import was removed and the repeated worker-only check records `exchange_imported=false`. Both attempts remain in local evidence.
- Local preflight read the unchanged V2 model checksum `a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2` and immutable model manifest checksum `15b55fa2c360729cebb37d8cb9cbe432249907873d7016b3b81929df173082f3`.

Full local receipts, exact source scopes and the separate final-head CI receipt are indexed in `docs/demo-paper-1h/VALIDATION.md`. The final ML audit pins the calibration manifest as well as weights and keeps clock/inactive-symbol/instrument rejections in the forecast funnel; four regressions reproduced these gaps before repair. Historical integration is not a latency certification or Demo matching test. Synthetic forced predictions are contract fixtures, not V2 market-hour trades.

A report-boundary regression reproduced a send timestamp taken before the REST queue and a displayed zero for unconfirmed funding. The send stage now begins after transport pacing; observed funding cash and funding completeness are separate, with unknown final funding/net blank.

The final account-scope audit reproduced eight missed-foreign-activity cases before repair. Preflight and REST gap recovery now inspect USDC, inverse, options and spot orders as well as USDT; nonzero unknown-symbol private position messages halt immediately. There is no foreign cancel/close path. The complete regression matrix passed after the repair.

## Current admission status

`LOCAL_IMPLEMENTATION_ONLY / DEMO_CONNECTION_NOT_TESTED / HOUR_NOT_STARTED`. The ignored Demo credential profile is absent in this worktree. No authenticated Demo request, market connection, order or hour was performed. The only required local setup is the ignored Demo profile described in the protocol; Start separately rechecks account identity, positions, permissions and current specs. This report does not assert that an untested account/region/API combination passes those checks.

No measured paired PnL, execution improvement or ML trading utility exists yet. Zero V2 selections remains a valid outcome, and no threshold is lowered to produce trades. No learned first-ready policy is implemented. Actual network execution can exceed 100 ms despite earlier scoped 20/5/250 ms offline passes.

The local-trigger design cannot guarantee closure after process/power loss. Unsupported private fee metadata, delayed funding confirmation, inaccessible reconciliation or unresolved exposure produce an incomplete result with unknown money retained; they are not silently treated as zero or reported as successful flat completion. Demo cannot establish live matching-engine quality.

The prepared Start command and frozen passport are in the protocol. Completion of this task does not execute them, alter PR59's eight-hour goals, permit live orders or merge main.
