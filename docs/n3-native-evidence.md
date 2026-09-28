# N3 native qualification — 28 September 2026

**W2.0 remains NOT_MET. N3.0 is not complete.** This iteration fixed two verified
recording/replay diagnostic defects and inspected the complete retained native
population. It did **not** implement or execute the requested six-variant native
module-cost harness, prove a latency improvement, or qualify a natural label path.
No market capture, model fitting, private/Demo/network test or long run occurred.

Starting PR61 `179af6dce3b890ffd726a8528808d3488d664605`; PR60 remains
`1a2d0f67da0331df7d2d134bffde6f64141c7898`. The audit/plan was committed first:
local `35282d7`, published `2e7966eb6f937f469cd3bf1c3249a8bc2718d887`.
Implementation: local `204dcbbad165675724b3961caedc2f19e8723ea6`, published
`1d28ce66fe8f351c8d3bc67ce6e0ebad7af8c35b`. Their trees are identical:
`b53e630bb5e8ec0e5c6da326e206cad63eb16414`.

## Verified native replay blocker

The original 300s W2 capture was checked against its untouched archived source.
All **144 archived source/script files**, pre-Start config/runtime/source hashes
and archive checksum match the freeze receipt. This is stronger than trying
current code against an old capture: the exact archived source and runtime pass
their strict checks, but cold replay stops at **configuration changed during
validation**, before native scheduler execution.

Only `paper_run_duration_seconds` differs: the launcher used unvalidated
`model_copy(update=...)` to record integer `300`; restored Settings validates
that float field as `300.0`. The values have different canonical JSON hashes.
Neither the raw manifest nor the strict source/config check was changed.

Two red-before/green-after regressions are retained:

1. Capture manifest creation previously accepted a configuration that strict
   restore cannot reproduce. It now validates exact replay-stable public config
   before recording; the smoke launcher supplies a correctly typed duration.
   Validation uses only supplied public values, with no environment or secrets.
2. A native clock mismatch could be overwritten by a scope-end mismatch during
   exception cleanup. The cursor now preserves the first sequence, expected
   observation and actual call, poisons further consumption and rethrows the
   original divergence. Successful clock consumption is unchanged.

These are correctness/diagnostic repairs, **not a performance patch**. They do
not make the preserved capture replayable or close any native label gate.

## Complete population and missing replay contract

`scripts/audit-native-qualification.py` reads and validates the entire primary
and supplemental capture. It is a read-only readiness audit, **not** the native
module-cost executor. It exits 2 with NOT_MET and records A–F as NOT_TESTED;
unavailable decision/portfolio/performance results remain null.

* Primary: **2,429,150 rows**, including **1,920,104 clock reads**, **115,444 market
  messages**, **299,824 scope rows**, **71,168 scheduler rows**, **2,422 dispatch
  rows** and every other control/await/service row. Chain/scopes pass, footer is
  present, final hash `2a8ba40f8f2508582382607573fc285bd6329ed41c60bbdd13cf78c1ede19a10`.
* Supplemental: **252,573 rows**, complete chain and matching capture identity;
  includes **57,156 external raw rows**, **57,138 external normalized events**,
  **36 external gap records** and **2 instrument records**.
* Every v4 clock row lacks module ownership. W2 uses engine/session clocks inside
  ordinary scopes. Turning hooks off changes clock consumption; consuming the
  remaining anonymous reads as no-ops would be an unsupported substitution.
* External gap/instrument dispatch has no primary sequence or total-order
  anchor. Receipt/processing timestamps in other rows do not recover a complete
  scheduler order, including gaps and tasks with no event timestamp.
* V2 feature creation wraps `_evaluate`, and polling/submission occurs in an
  independent heartbeat. This adapter schedule and worker/relay dispatch are
  not present in the native scheduler tape. The original script bytes are frozen
  in the separate receipt, but freezing code does not record its scheduling.
* `OfflineScheduledReplay` strictly consumes ordinary scopes/clocks, but stepping
  saved coroutines does not itself measure native loop lateness or real IPC.
  Its wall duration must never replace either acceptance metric.

Thus fixing duration typing alone cannot enable the requested A–F comparison.
The absent module/async ownership cannot be inferred retrospectively. The
remaining implementation is a native controlled driver plus prospectively
recorded module boundaries, external dispatch and V2 request/reply ordering.
It must first prove complete ordered consumption and ordinary semantics on an
offline native fixture. A new uncontrolled market capture would not repair this
missing driver and is blocked by the user's controlled-gate requirement.

The new report retains distinct population/scheduler/clock hashes. Hash basis:
SHA256 of the ordered concatenation of the verified 32-byte row hashes.

| Identity | SHA256 |
|---|---|
| Full primary population | `0ef0fde7bcaaf4c5f0521aa01eae89487e5ee8df9f8a73cfcd35263f3c3828d5` |
| Clock population | `6420b8831740b689151035bb5edf3cc204cc38353202b2b8fdbbe17fa036ef3b` |
| Scheduler/scope/control population | `6e0e70549ea6fd4bd8009a1d2bea51f831654e0cc20d992b2ea9707492da8b1a` |

## Per-item native attribution

All **488/488 forecasts** join to their exact primary source sequence, symbol,
receipt timestamp and event ID, plus the corresponding epoch-bound feature row.
All **488 critical paths telescope exactly in integer nanoseconds**. Every joined
row is retained in the evidence bundle. This uses original native timestamps;
no new timings, logical-clock substitution or sum of marginal percentiles.

The actual p99 item is SOLUSDT short, source sequence **1670724**, event **m77922**:

| Contiguous interval on this one item | ms |
|---|---:|
| Source receipt → features ready | 15.8920 |
| Features ready → prediction end | 272.5930 |
| Prediction end → adapter receipt | 19.7457 |
| Adapter decision | 0.0126 |
| Exact end-to-end duration | **308.2433** |

The maximum item is ONDOUSDT short, sequence 1765629, event m82356:
**6.1985 + 335.7340 + 35.5647 + 0.0136 = 377.5108ms**. Eight forecasts exceed
250ms. The large middle interval includes the probe/parent queues, IPC and
prediction. The native capture lacks the boundaries required to divide it
further. The separate previous instrumented logical run cannot supply those
missing native boundaries. This localizes the measured interval without claiming
a specific queue or codec operation is its cause.

True allocation throughput, per-kind canonicalization/hash/gzip/IPC/file-write
measurements and instrumentation tax remain NOT_TESTED. Retained-block deltas
are not re-labelled as allocations. No clock compression, queue/limit change,
GC disablement, speculative optimization or trade-model change was made.

## Gates and stop decision

| Gate | Status | Evidence / reason |
|---|---|---|
| N3 file audit / original archive binding | MET | Plan first; complete inventories and 144 archive/script matches |
| Config and first-divergence regressions | MET | Two failing-before receipts, 42 focused passing-after tests |
| N3.0 complete native six-variant executor | NOT_TESTED | Required driver/ownership/async boundaries are absent; readiness audit is not completion |
| N3.1 full allocation/codec/IPC attribution | NOT_TESTED | 488 exact native coarse paths joined; fine boundaries and true allocation source absent |
| N3.2 controlled W2.0 | NOT_MET | No eligible native comparison; preserved 24.4621ms loop / 308.2433ms adapter still fail |
| N3.3 single 900s public-paper capture | NOT_TESTED | Not started because controlled gate is open |
| N3.4 native executable labels | INCONCLUSIVE | No current native path/output/clock parity |
| N3.5 independent dataset | NOT_MET | Existing frozen floors unchanged; 11 observations / 1 logical derivative label |
| N3.6 CrossVenue economic influence | INCONCLUSIVE | No native prepared population or required coverage; telemetry only |
| N3.7 maker hypothesis | NOT_TESTED | NO_SUPPORTED_HYPOTHESIS_YET; 4490 candidates / 808 fills, negative means; no independent hypothesis test |
| N3.8 V3 training/promotion | NOT_TESTED | Dataset blocks fit; no model or placeholder artifacts |
| N3.9 live paired adapter/run | NOT_TESTED | Offline harness MET is inherited; no new downstream expansion |
| N4.0 production Demo qualification plumbing/run | NOT_TESTED | Offline contract MET is inherited; no new downstream expansion or private/order calls |
| N4.1 8h eligibility | NOT_MET | Upstream proof incomplete; no long run |

The sequence remains: controlled W2.0 → one native 900s natural path → native
label parity → independent multi-capture dataset → trained/calibrated V3 with
LOSO and untouched test → short shadow latency smoke → owner-authorized paired
paper technical run → optional owner-authorized Paper/Demo execution qualification
→ 8h eligibility. Optional Demo absence cannot be silently reported as a passed
Demo gate. No downstream result can replace missing upstream proof or revise
frozen floors retrospectively.

## Validation and retained files

Full Windows preflight: **1671 passed in 226.71s**. JS syntax checks pass.
Expanded Windows target suite: **570 passed in 68.93s**, including
ML/spawn/codec/CrossVenue/paired/Demo, manifest and native replay regressions.
Linux implementation CI: **1657 passed / 6 skipped**, with JS syntax passed. Exact implementation CI is
[36417097085](https://github.com/branch-danya-dev/scalp-bot/actions/runs/36417097085);
final CI receipts are retained with the handoff, and exact final-head status is
reported in PR61. Passing tests do not close W2.0 or establish profitability.

Evidence: [n3-native/](wave2-evidence/n3-native/). The original raw primary,
supplemental, source archive, failed instrumented captures and historical
negative outcomes remain untouched at their inventory paths. The readiness audit
also preserves its SQLite source join index locally. The failed archived-source
witness is preserved separately; it did not reach scheduler execution.
