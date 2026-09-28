# W2.0 capture-load continuation — 28 September 2026

Implementation: `9adee8faa35aea5d7e58097aac8bb27cbb311c51`, on PR61 above exact PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main and PR60 are unchanged. The [file audit and implementation plan](w20-load-plan.md) preceded implementation.

The new 300s public paper capture has complete primary/supplemental chains, zero recording losses/backpressure, and passes its loop/adapter latency budgets. It has **zero prepared intents and zero ordinary fills**: **INCONCLUSIVE**. The fixed historical full-clock stress workload still fails latency. Neither observation closes W2.0 or authorizes a paired/8h run.

## Implementation and regressions

- InputWriter sends one bounded detached batch to a separate hash/JSON/gzip process. FIFO chain receipts preserve ordering across batches and independent chains. The original 32 MiB retained-byte limit and 32768-row queue are unchanged; in-flight bytes stay charged until file write. Failure/death/drain diagnostics are explicit, and shutdown is bounded.
- Standard concatenated gzip members retain the same decoded JSONL/schema/canonical hash semantics. Codec isolation adds one child process per input/research writer. Startup failure invalidates the attempt; no silent fallback exists.
- Clock reads use compact scalar deferred rows; JSON containers are constructed in the writer. All observations and previous conservative memory charges remain. Only owned acyclic capture holders are excluded from cyclic GC; runtime GC is unchanged.
- Fixed trade-flow buckets are unrolled with the same input order and built-in sums. A redundant copy of an already detached scenario snapshot is removed. The same canonical stdlib encoder configuration is reused. Golden trade-flow, nested detachment, finite-float/Unicode and concurrent hashing tests preserve behavior.
- Smoke stops new activity after writer/research failure. Direct transport journal events now count backpressure/discarded messages; these events bypassed the previous `_emit` monitor. Both defects have failing-before/passing-after receipts.
- The historical profiler explicitly supports engine/broker/**warmed-session** clock capture. Early helpers omitted warmed-session clocks; those results remain labeled partial coverage and are not used as full-clock success evidence.

Windows/Python 3.13 full preflight: **1575 passed in 221.13s**; both JavaScript syntax checks passed. The Windows CI job now also covers the codec/spawn/capture/smoke regressions. [Red/green and full-suite receipts](wave2-evidence/w20-load/validation/) are retained. CI status belongs to the exact published head, separately from these local receipts.

## Fixed historical diagnostics

All unprofiled variants consume the same 25045 source events: offset 60s, duration 58s from the failed PR60 prefix. This is a logical scheduler with a V2 probe at each evaluation, not native receive/parse, output parity, a natural fill, or training data. The public smoke uses its existing 10-second feature grid, so its timings are not a controlled A/B against this stress workload.

| Full-clock variant | Accepted/written rows | Rejected | Loop p99 ms | Data→adapter p99 ms |
|---|---:|---:|---:|---:|
| Small gzip-only child | 346522 | 120880 | 37.0755 | 388.7561 |
| Larger gzip-only batches | 467402 | 0 | 42.3142 | 658.1167 |
| Full codec, ordinary deferred objects | 462650 | 4752 | 43.7197 | 845.3846 |
| Large thread-only diagnostic | 460401 | 7001 | 41.7335 | 587.0780 |
| Full codec, scalar deferred clocks | **467402** | **0** | **44.5126** | **682.9072** |

The final fixed-load writer high-water is **24003250 bytes**, below the unchanged 33554432-byte bound. Its 467402-row hash/sequence prefix passes independently; it intentionally has no native capture footer/manifest and is not a complete dataset. Loop 20ms and adapter 250ms budgets remain **NOT MET**. Ordinary decision and portfolio hashes are identical across the unprofiled variants; all have zero closed trades.

The retained [attempt index](wave2-evidence/w20-load/attempts.json) includes all 12 engine attempts, including profiling, partial-clock experiments and failed variants. GIL-interval changes, handwritten clock hashing, gzip-only isolation and the thread-only override were not retained. Do not infer a speedup from partial-clock measurements. Source hashes for each variant are in the receipts; the final benchmark precedes the cosmetic codec startup-error wording and smoke transport-monitor edits in `9adee8f`.

The remaining stress bottleneck needs separate investigation: due-callback p99 30.8527ms, evaluate p99 38.0472ms and source-queue wait p99 369.1905ms, versus prediction p99 0.9318ms. These stage observations are not proof of a single cause. Preserve scheduler/strategy semantics and compare identical complete event populations before any further optimization.

## One native 300s unified capture

Source was committed and archived before Start. No restart, extension or setting relaxation occurred. All execution used PaperBroker/public market data; no Demo/mainnet/private order call was made.

| Observation | Result |
|---|---:|
| Primary input rows | 2592864 accepted = written |
| Primary market messages / clock reads | 125288 / 2079874 |
| Supplemental W2 rows | 286086 accepted = written |
| Primary / W2 writer high-water bytes | 10863008 / 7614535 |
| Backpressure / discarded messages / writer drops | 0 / 0 / 0 |
| Header/footer/hash/scope checks | Passed, no issues, 0 missing receipts |
| Loop p99 / max | 17.3199 / 217.7550 ms |
| Data→adapter p99 / max | 109.0299 / 124.6696 ms |
| Market detach/enqueue p99 | 0.2455 ms |
| V2 shadow forecasts | 476, all probability abstentions |
| First-prepared / natural fills / closed trades | **0 / 0 / 0** |

Normalized cross-venue replay passes. No prepared event exists to test a native prepared-context join, executable label, registry decision or FIRE→fill→exit path. Primary validator still correctly reports `parityReady=false`: complete hash/scope checks are not native output parity. ML incremental latency is not measured by this single shadow run.

Maker shadow: **2214 virtual candidates**, 483 with any virtual fill, 1283 fill fragments. Mean net markouts at 100/500/1000ms are **−0.042508 / −0.047629 / −0.045863 USDT per complete fragment**. Window-gap censoring is 4/10/12. At each of 500/1000ms, final candidate outcomes also retain one `missing_depth` and one `transport_cancelled` censor, absent from the timed markout-event stream; [final-outcome counts](wave2-evidence/w20-load/maker-final-outcomes.json) include them. These are overlapping shadow observations, not portfolio PnL, and not independent positive evidence. All candidates and censored outcomes remain in raw data. Maker execution/ML and applied registry risk remain disabled.

## Provenance and next gates

- Source SHA256: `f3ceff6b63250811fb0dfbd3a06a743379537e7425fbb34fc5dc75dd522f44e1`.
- Config SHA256: `3aaba312e78b6b7d50daff788a080f26223dddeec7ecdb2a6717a6aeffc32a88`.
- Runtime SHA256: `af5748c639af3cc554ab410df665b424d29d51dc1f2a9c93b2da8df0dd4ca9d5`.
- V2 model SHA256: `a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2`; weights and `.55` unchanged.
- Pre-Start source archive SHA256: `170b4739ef6f3a055a04468d871cda94c89d9f54c98af5ec84bf7d07dc9a3146`.

Source/config/runtime/model match the pre-Start freeze. Git metadata was unavailable inside the elevated public-feed process; the separate pre-Start clean-checkout receipt binds commit `9adee8f` to the identical on-disk source hash and archive. No Git identity is fabricated in the capture manifest.

[Native evidence](wave2-evidence/w20-load/paper-300s-evidence.json), [freeze](wave2-evidence/w20-load/paper-300s-freeze.json), [primary integrity](wave2-evidence/w20-load/paper-300s-primary-integrity.json), and [all local raw hashes](wave2-evidence/w20-load/local-raw-inventory.json) preserve the exact attempts. The original PR60 overflow and all subsequent failures remain available.

Next: isolate the remaining stress callback/queue latency; keep natural-fill and native executable-label replay gates open. Another bounded market observation needs a separately chosen interval, not an automatic repeat loop. Training/promotion requires independent complete labeled captures and frozen splits/calibration/test; maker execution requires independent positive economics. Paired 30–60m integration and 8h remain unauthorized by the evidence.
