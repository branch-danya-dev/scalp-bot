# W2.0 callback continuation — 28 September 2026

The [file audit and plan](w20-callback-plan.md) were committed as `8c96db4` before changes; implementation is `523b16adb4555cf38cc4f6fde44ca960b99dece4`. This continuation stays on PR61 above exact PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main, PR60, V2 weights/.55, economics, risk, capture limits and scheduler/probe pacing are unchanged.

## Changes and regression evidence

Forming context now scans the retained tape once for minute count/latest and collects only the causal 15-second micro window. That window is sorted once; its 5-second subset retains stable timestamp ties and the same arithmetic. It no longer sorts the full minute and both micro windows separately. Book-flow 5/15/60-second windows are partitioned in one traversal, retaining input order and builtin `sum` behavior. No incremental/approximate sums or input pruning were introduced.

Before the fixes, deterministic work-bound regressions failed: 7001 timestamp reads for 1000 ticks (budget 3000), and 3000 book-flow event visits (budget 1000). The other 56 new semantic cases already passed. After fixes, all 164 targeted tests passed, including stable out-of-order/tied timestamps, future events, inclusive cutoffs, unusual prices, cancellation-sensitive floating sums, clock reads and existing flow/strategy/feature tests. Both new regressions also enter Windows CI. Receipts preserve the failing-before/passing-after sequence.

The bounded stage profile identified repeated tape work. Inclusive spans include instrumentation, and Windows thread CPU readings are quantized; these are diagnostic attribution, not acceptance timing. Three alternative trade-flow implementations were tested on fixed recorded tapes and were slower than the existing one; none was retained. All variants and raw measurements remain under `work/w20-callback`.

Full local Windows/Python 3.13 preflight: **1633 passed in 222.47s**. Both JavaScript syntax checks passed. GitHub Linux/Windows results are reported for the exact published head separately.

## Fixed historical workload

Both unprofiled runs use the identical offset60/duration58 workload, 25045 source events, 3561 evaluations and full engine/broker/warmed-session clock recording. Workload SHA256 is `0c8036ca7b06406e9d8c2933aa522174f4df87086443e79b9fc71004079c6b85`; harness SHA256 is `6fa51919e8a55b359e06fe9e997a126d8db7c08c075b34b4e9915a46f94a2855`.

| Metric | Fresh baseline | Fixed |
|---|---:|---:|
| Accepted = written rows | 467402 | 467402 |
| Rejected rows | 0 | 0 |
| Writer high-water bytes | 22801229 | 32624223 |
| Loop p99 / max ms | 32.3761 / 84.9749 | 31.3096 / 116.4851 |
| Evaluate p99 / max ms | 27.4496 / 69.9270 | 24.9096 / 91.4429 |
| Data→adapter p99 / max ms | 352.6503 / 543.0211 | 291.1001 / 431.4148 |
| Delivered forecasts / coalesced | 1691 / 1870 | 1704 / 1857 |

The fixed run still **FAILS** the 20ms loop and 250ms adapter p99 budgets. Writer high-water is close to the unchanged 33554432-byte bound. The observed p99 reduction is one sequential before/after measurement, not a statistically established speedup; maxima and writer headroom did not improve. Prior final-source measurements also show host/load variability (44.5126/682.9072ms in the previous continuation versus this fresh baseline 32.3761/352.6503ms). No favorable-run selection or market retry loop is used.

Ordinary decision SHA256 remains `0bc53475c67e0c93636250272f59d8a8ccce965bee5cf7a74dedb4fa7f6a77d8`; portfolio SHA256 remains `efade72b47b94d8636890fac3238738f005690496c3a3406cdba6e5fb5cc7b0d`. Both have zero closed trades. The logical scheduler and per-evaluation V2 probe differ from native smoke pacing. This failed historical prefix is diagnostic only, never training data, native parity or natural-fill evidence.

Independent validation checked every row's sequence, previous hash and canonical hash in both historical streams. Both 467402-row prefixes end at the identical hash `a2e18972d74d2d3e1b84ff0fd3fd0410059dd58eba24eb0fe1a23d5a6ad26af1`, including 364261 clock reads each. Neither has a native footer; both remain diagnostic prefixes.

## One native 300s unified capture

The source/config/runtime/model were frozen and the source archived before Start, on `523b16a`. Exactly one 300-second public paper window ran, with no restart, extension or setting relaxation. It recorded **2429150 primary + 252573 supplemental rows**, accepted = written, zero rejects, backpressure, discarded messages and critical writer drops. Both writers drained and their codec processes exited. Primary/W2 high-water bytes were 11367815/7654673.

Native loop p99/max was **24.4621/335.2288ms**; data→adapter p99/max was **308.2433/377.5108ms**. Both p99 budgets **FAIL**. The 488 V2 shadow responses all abstained; V2 remains a load probe, not a V3 model or ordinary admission gate. There were **0 prepared intents, 0 natural fills and 0 closed trades: INCONCLUSIVE_NO_FILLS**. This different market period is not a controlled speed comparison with the prior native capture. No retry was launched to replace the unsuccessful result.

Primary header/footer/hash/scope checks passed, with zero issues/missing receipts: 115444 market messages and 1920104 clock reads. Supplemental integrity and normalized cross-venue replay passed; executable-label replay is **NOT_TESTED**, with zero labels, and trainingReady/parityReady remain false. Source/config/runtime/model matched the pre-Start freeze. The clean pre-Start receipt binds the commit/source even though Git metadata is unavailable inside the elevated capture process. Exchange→receipt wall-clock differences include skew and are not physical latency; budget metrics use local monotonic timing.

Maker shadow retained 2276 candidates, 325 with any virtual fill, and 744 fill fragments. Mean cost-adjusted markouts at 100/500/1000ms were **−0.048344/−0.053589/−0.051542 USDT per complete fragment**. Final outcomes retain all censors: 6/4/4 markout-window gaps and two transport-cancelled fragments at each of 500/1000ms. These overlapping observations are not portfolio PnL or independent positive maker evidence. Registry risk, maker execution and model promotion remain disabled.

- Source SHA256: `b6d464c6a378733331937668cfac197d9360b5c326de11d15bc48487f4bf1a7e`.
- Config SHA256: `ac49297b814ff3801aa734978fe0fd802f5416d9ada36c65ab1c3faad9bbe9a0`. The only config difference from the previous 300s capture is `session_dir`.
- Runtime SHA256: `af5748c639af3cc554ab410df665b424d29d51dc1f2a9c93b2da8df0dd4ca9d5`.
- V2 model SHA256: `a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2`; manifest SHA256: `15b55fa2c360729cebb37d8cb9cbe432249907873d7016b3b81929df173082f3`.
- Pre-Start archive SHA256: `e2326c7a813eaaa2e8967c45ac8a6524e45223f2ddaa925ce4542c27d9e9a636`.

## Remaining gates

W2.0 remains open: complete causal recording is necessary but does not establish a natural PreparedIntent → EconomicPlan → FIRE → fill → exit. Full-load and native latency, natural fill/exit and native executable-label replay must be resolved without weakening thresholds or selecting successful hours. Next investigate bounded callback duration, adapter queue age and codec drain headroom against these preserved traces before choosing another market interval. Maker execution/ML, applied registry risk, paired 30–60m integration and 8h remain gated. All prior failed captures remain preserved; this continuation adds a separate [evidence directory](wave2-evidence/w20-callback/) with source receipts, all raw hashes, red/green tests and failed latency results. No Demo/mainnet orders or main/PR60 changes occurred.
