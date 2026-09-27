# Trade plan diagnosis — PR58

27 September 2026. Base ledger/code: `1d7d9aba6c378cba70403d10234b26d9f7df367e`. Diagnostic groups frozen in `62c2853` before outcomes. [Execution log](docs/pr58-trade-plan/COMMANDS.md), [protocol](docs/pr58-trade-plan/DIAGNOSTIC_PROTOCOL.md), [task](docs/pr58-trade-plan/TASK.md). Existing reports remain historical evidence, not overwritten conclusions.

## 1. Ordinary decisions and losses

Parallel preparation correctly exposes additional ready scenarios, but readiness did not establish a durable, cost-paying directional advantage in this capture. Reclaim/absorption or a short micro response admitted countertrend reversals in ENA/XRP/ZEC. In ETH and the later ZEC long, a favorable move either did not pay the complete lifecycle or was not fully observable. A distant projected target and conditional reward/risk are not a measured probability of reaching it.

All 21 variant ledger observations represent 11 distinct execution paths and 9 exact episode identities. The table includes every path, its original entry plan, market/flow basis, first ready, admission/fill/exit, individual fees, actual execution modes and secondary causes. [Attribution](docs/pr58-trade-plan/trade_failure_attribution.csv), [reviewed causes](docs/pr58-trade-plan/ATTRIBUTION_NOTES.md), [all 5/15/30/60/120s quote paths](docs/pr58-trade-plan/trade_quote_paths.csv). No future target after an earlier stop is counted as success. Top quote extrema are bounds, not executable alternative fills.

| single_legacy → parallel_legacy component | Delta USDT |
|---|---:|
| Newly executed exact episode identities | -19.37130905025924 |
| Disappeared identity | +10.165085331358451 |
| Retained identities: ETH entry/path; same quantity | +0.3669383751686368 |
| Total | -8.83928534373215 |
| Arithmetic residual | 0 |

The disappeared single ZEC short and first parallel ZEC short are different structural objects within a nearby market excursion; exact identity reconciliation does not make them independent opportunities. [Cash decomposition](docs/pr58-trade-plan/routing_delta_reconciliation.csv). The old totals remain -34.3670914051 / -43.2063767488 / -43.5300317630 for single_legacy / parallel_legacy / parallel_quote_tape. There is no claim that individually removing a losing execution adds its loss to a realizable portfolio.

Positive control ZEC01 remains +0.341814, including a legitimate post-entry partial. ZEC06 direction initially worked (10.061bps favorable movement, +0.324576 partial), but gross4.465841 was below fees4.797382 after the runner's immediate taker close. The stop/partial policy is unchanged. ETH has an85.8466s gap and cannot support a complete exit-opportunity claim. Preparation waiting0.76–93.26s is distinct from order latency.

All predeclared ready controls are retained, not only fills: parallel breakout25 ready/24 covered includes7 positive and17 nonpositive illustrative60s outcomes; rejection12/12 includes0 positive. These use the previous13bps top-quote diagnostic, not individual ledger accounting or a new entry filter. [Control summary](docs/pr58-trade-plan/ready_control_summary.csv), [original immutable T01–T06](docs/pr58-trade-plan/original_broker_controls.csv).

### Confirmed technical correction

In parallel ZEC07, raw batch21025769 was received22.575003ms before the position opened and processed13.047997ms afterwards. Its55 historical ticks retrospectively confirmed a maker partial for a later order. The engine now excludes such pre-entry tape from maker-exit evidence, while retaining ticks in market state, book marking, protective stops and current post-entry confirmations. A failing regression reproduced the bug before the fix; fresh-tape and protective-stop controls pass after it. No trading parameter changed.

This invalidates that specific old execution path as evidence of realizable money. The original ledger and baseline remain intact; a corrected whole-portfolio replay was not performed, and no improved PnL is claimed from this fix.

## 2. Existing ML V2 plan and economic ranking

No training, threshold, calibration, label or policy changes. Verified dataset/model hashes and the fixed30/15bps/30s/100ms/100USDT plan. Reconstructed real OrderBookState paths match original entry/exit/first barriers for all13665 train/calibration/validation rows. Published test2847 and purged278 are excluded from every new diagnostic/ranking container.

| Validation outcome | Windows | Mean gross USDT | Mean fees USDT | Mean net USDT |
|---|---:|---:|---:|---:|
| All | 3023 | -0.027686 | 0.104004 | -0.131690 |
| Timeout | 2770 | -0.016740 | 0.103604 | -0.120343 |
| Stop first | 239 | -0.174510 | 0.108395 | -0.282906 |
| Target first | 14 | 0.313003 | 0.108282 | 0.204721 |

Timeout has81 positive and2689 negative net payouts; it is neither zero nor inherently bad. The30bps target is median6.0345 times the pre-entry forming range. Executable MFE before the original exit has median0.0291bps and p9513.4665bps. Both fill slippages are already embedded; fees are deducted once. Dated instrument constraints, depth and gap exclusions remain those of the unchanged label engine.

Higher CatBoost scores rank target_first more often, but do not produce better money selection. Top1/5/10% mean net is -0.157506/-0.143500/-0.143044 versus -0.131690 overall. Upper decile target frequency2.318% exceeds0.463% overall, but mean net is worse;96.03% of that decile is ENAUSDT, and top5% is entirely ENAUSDT. All decile means are negative. The29 global60s buckets are dependent groups, not proven independent samples. Fixed top-slice group-bootstrap intervals remain negative; short history and concentration limit generalization.

Logistic top10% mean -0.141192; the unchanged rule0.55 selects258 windows with mean -0.147801. CatBoost/logistic select0 at0.55; no-trade cash0. Tied prior/rule ranks have no predictive within-tie order. Overlapping label payouts are never summed into portfolio PnL. [Split/symbol/side/state tables](docs/pr58-trade-plan/ml_plan_diagnostics.csv), [validation ranks and uncertainty](docs/pr58-trade-plan/ml_score_payoff.csv), [ML diagnosis](docs/pr58-trade-plan/ML_DIAGNOSIS.md).

The result supports a combination of plan/population mismatch, costs, insufficient economic discrimination and narrow data coverage. Useful target and timeout outcomes do exist; V2 does not isolate a net-positive choice here. Abstention remains justified. Among all26 causal development ready events,23 have an older-than1s or absent preceding grid sample. This proves a sampling-time mismatch, not missed profitable impulses.

## 3. Isolated performance

Final measured series and stage tables are recorded separately in `docs/pr58-trade-plan/performance-isolated.json`. The original20/5/250ms budgets apply. Measurements include all responses and stale rejections, recorder, every raw input and bounded-mailbox outcomes. No own training/replay/tests/indexing/download runs overlap measured series; foreign processes remain untouched. Local isolation does not mean an idle machine.

Original isolated harness still exceeded budgets. Three handler pauses297–347ms overlapped generation2 GC for294–345ms. Loaded archive sweeps took172–182ms versus4–10ms in the empty process. This is a preload/harness artifact, not evidence that predict is slow. The harness now isolates the immutable pre-runtime archive from cyclic collection; runtime engines remain normally collected. It also records an explicit bounded source queue and causal last-applied-symbol timestamps. No raw delta is skipped, freshness extended, recorder disabled or queue budget enlarged.

A scoped1ms Windows timer profile is measured explicitly with idle before/after controls; it is not a production launcher change. Default-profile failures remain evidence. Old and new data-age semantics are identified separately and the old metric is retained alongside the stricter causal metric. Both fixed windows completed all three alternating pairs at1x and4x:24 runs. Every20/5/250ms guard passed in this measured profile; all decisions and portfolios match off/shadow and the corresponding unchanged-harness run. The API timer request did not demonstrably improve idle scheduling: window1 p99 was15.0626ms before and15.0629ms after; window2 was15.1531/15.2007ms. Do not attribute the improvement to that request. Passing a250ms adapter budget alone does not certify the label assumption of100ms to entry, and abstentions are not proposals.

### Measured comparison and stages

| Series | Loop p99 range, ms | Largest shadow addition, ms | Data→adapter p99 range, ms |
|---|---:|---:|---:|
| Fixed harness, normal1x | 14.4998–15.1695 | 0.0608 | 54.5361–63.0712 |
| Fixed harness, burst4x | 15.3397–18.4703 | 2.6548 | 64.4244–216.0147 |

Original isolated series had five loop-budget violations and three adapter-budget violations (seven distinct runs), with adapter p99 up to419.20455ms. Previous unisolated reports and every failed repeat remain in [performance-isolated.json](docs/pr58-trade-plan/performance-isolated.json). Removing concurrent own jobs alone did not remove the issue. This is a comparison of complete measurement configurations; it does not assign a precise causal percentage to each harness change or background scheduling variation.

After correction,22,126/22,158 submitted jobs returned forecasts (99.856%);32 were explicitly coalesced, capacity drops0, worker errors0, stale/TTL rejections0 and proposals0. Every returned forecast abstained. All raw inputs were processed (13,691 and13,633 per run), no recorder rows were dropped, no writer errors. Coalesced jobs have no invented successful-result latency. Raw stage files preserve each input/evaluation/prediction ID; even unsuccessful responses enter terminal timing.

Across runs the largest stage p99 values were: generator27.647ms; source queue203.604ms; due callbacks6.245ms; actual market-state update0.482ms; context build0.252ms; individual strategy2.061ms; features0.116ms; parent worker queue14.068ms; dispatch→predict7.030ms; predict1.018ms; return15.804ms; adapter0.031ms. These are envelopes over different samples and must not be added. [All counts/p50/p95/p99/max](docs/pr58-trade-plan/performance-stages.csv) and linked raw samples supply the event-level comparison. Queue depth peaked at272 of the unchanged512 capacity. Runtime GC remains enabled; its maximum observed pause149.400ms was not suppressed or excluded.

The maximum complete path was224.760ms: source sequence601792, ZECUSDT, window2 burst repeat2, prediction1525. Generator9.914ms, source queue198.095ms, predict0.552ms; exact timestamps and GC intervals are in performance-final/worst-path-witness.json. Only4.803ms of that particular queue interval directly overlaps a recorded GC pause, so the whole remaining backlog is not attributed to that pause or to prediction. Earlier archive-GC localization and this residual queue observation are distinct evidence.

For V2 burst repeats,3.96%/4.44%/6.34% of delivered forecasts exceeded100ms to adapter. This does not satisfy the label assumption of entry after100ms even though250ms p99 passes. No labels were regenerated, no forecasts forced into trades, and actual exchange execution was not benchmarked. Whole-series global CPU p50 was38.64%/46.95%, maximum62.91%/92.32%; timed per-run pressure is also saved. This was isolation from own competing work, not an idle or controlled-host guarantee.

## 4. Boundaries and next decision

There is no proven ordinary/hybrid portfolio improvement, independent external holdout utility or live execution admission. ETH gap, incomplete teardown, fixed captured membership, uncertain nearby economic episode mapping and unknown historical handshake root causes remain. `close_timeout` is not a repair of all handshake failures. Recovery/epoch/freshness and shared risk/one-owner protections remain intact.

Exactly one proposal is documented: evaluate the unchanged plan at a first causal scenario-ready event instead of the arbitrary10s both-side grid. [Proposal and rejection criteria](docs/pr58-trade-plan/NEXT_POLICY_HYPOTHESIS.md). It needs separate approval of an offline protocol and a fresh untouched holdout. It is not implemented, trained or enabled. No new ordinary filter or V3 was introduced.

PR59's next-paper-8h-v1 goals and28800s duration are unchanged: ordinary positive net; if separately admitted, hybrid positive net and positive incremental result at matched risk. launch_authorized=false; selected_mode=null. No new market connection/run, order, ML trading, scheduling or main merge is authorized by completion of this diagnosis.


## Verification and evidence

[Evidence index](docs/pr58-trade-plan/EVIDENCE_INDEX.md) maps requirements to reproducible scripts, original source lines/sequence and local checksums. Exact first-ready decision snapshots were recovered for all21 observations; [ready versus entry context](docs/pr58-trade-plan/ready-vs-fill.csv) retains actual differences, including ZEC06 strongly_aligned→aligned. All seven public-trade receipt witnesses reproduced exactly; the two pre-entry observations are the same defect in parallel_legacy and parallel_quote_tape.

Local full preflight:1404 passed in253.66s; targeted guard/clock/diagnostic tests23 passed; combined stage/diagnostic tests14 passed. Final trace tests after adding three output-only stage metrics:3 passed. JS syntax and whitespace checks pass. Published-head Linux/Windows CI is recorded with its exact commit/run/jobs in the local final-ci.json receipt and PR58 update; a previous head's CI is not treated as final-head evidence.

Implementation blocks:62c2853 frozen protocol;81f5170 attribution/ML diagnosis;1fa6f37 maker-exit causal guard;6cc28d8 archive-GC isolation and stage tracing. Final evidence/publication is a separate commit. Source/config/model hashes, old failed attempts, regression before/after and command log are retained. Protected dataset/model/control hashes match their initial values; only the documented engine/benchmark source changes are expected.

CI portability note: the first published Linux job failed only the new subprocess GC test's assumed initial frozen state (1395 passed,3 skipped,1 failed); Windows ML236 passed. Test setup was isolated explicitly and a guard-preservation control added (4 local tests pass). Benchmark and production source are unchanged. The harness continues to reject a caller's preexisting frozen population; it does not silently unfreeze another environment. Original failed logs are retained with the final CI evidence.
