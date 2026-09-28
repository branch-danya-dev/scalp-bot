# W2.0 callback continuation — 28 September 2026

The [file audit and plan](w20-callback-plan.md) were committed as `8c96db4` before changes. This continuation stays on PR61 above exact PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main, PR60, V2 weights/.55, economics, risk, capture limits and scheduler/probe pacing are unchanged.

## Changes and regression evidence

Forming context now scans the retained tape once for minute count/latest and collects only the causal 15-second micro window. That window is sorted once; its 5-second subset retains stable timestamp ties and the same arithmetic. It no longer sorts the full minute and both micro windows separately. Book-flow 5/15/60-second windows are partitioned in one traversal, retaining input order and builtin `sum` behavior. No incremental/approximate sums or input pruning were introduced.

Before the fixes, deterministic work-bound regressions failed: 7001 timestamp reads for 1000 ticks (budget 3000), and 3000 book-flow event visits (budget 1000). The other 56 new semantic cases already passed. After fixes, all 164 targeted tests passed, including stable out-of-order/tied timestamps, future events, inclusive cutoffs, unusual prices, cancellation-sensitive floating sums, clock reads and existing flow/strategy/feature tests. Both new regressions also enter Windows CI. Receipts preserve the failing-before/passing-after sequence.

The bounded stage profile identified repeated tape work. Inclusive spans include instrumentation, and Windows thread CPU readings are quantized; these are diagnostic attribution, not acceptance timing. Three alternative trade-flow implementations were tested on fixed recorded tapes and were slower than the existing one; none was retained. All variants and raw measurements remain under `work/w20-callback`.

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

## Remaining gates

W2.0 remains open: complete causal recording is necessary but does not establish a natural PreparedIntent → EconomicPlan → FIRE → fill → exit. Full-load latency, natural fill/exit and native executable-label replay must be resolved without weakening thresholds or selecting successful hours. Maker execution/ML, applied registry risk, paired 30–60m integration and 8h remain gated. All prior failed captures remain preserved; this continuation adds a separate evidence directory.
