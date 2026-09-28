# N3 production wiring and spawned request replay — 28 September 2026

**The main deliverable is incomplete. Production N3.0 and W2.0 remain NOT_MET.**
This continuation wires and tests production recording roots, splits the W2
observer, extracts the frozen probe, adds real spawned request replay and a
disk-backed structural validator. It does not provide the complete-session
production A-F executor requested in P8/P9. No production module cost, latency
improvement, market qualification or natural-label proof is claimed.

Baseline is exact PR61 `23d35252224f1dd472aaee8282b82e7796e3edf2`, stacked on
unchanged PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main and PR60 are
not write targets. The [audit and plan](n3-production-v5-plan.md) preceded code:
local `789579a`, published `1abe9218a644645d973c847c831a5c4be0132001`.
Implementation: local `e393eb5`, published `024bbc1a173f4f042dede2759a165816e7221dc1`,
identical tree `3d094af4bea64f9f8bfc564b0343cbe15d92e81b`.
Model binding: local `9552ae8`, published `780b59294806c2991a0e489bcb5d9812fef33d51`,
identical tree `5ed735c42a002a359119a47e03ab95d651e7ec4e`.
Evidence is committed separately.

## Implemented and exercised

* `NativeDispatch` records actual coroutine creation, first dispatch, each
  suspension/resumption, completion/cancellation and synchronous completion
  callbacks. Cancellation before first execution has a terminal. Parent/module
  identity is explicit; an inherited ContextVar cannot authorize an unknown
  asyncio task to read clocks. Completed coroutine frames are released.
* Bybit receive/process, engine service/symbol/evaluation/timer roots and source
  gather children use the opt-in registry. Source-await and scope markers feed
  the native sink. Runtime engine/session/broker time routes to the active owner;
  transport wall nanoseconds remain integer `time_ns`, without float conversion.
  Recorder/codec envelopes retain the independent diagnostic clock.
* Immutable ingress identities use independent source-lane counters. Strategy
  references use the last **applied** source message, not a later queued one.
  Native event-evaluation IDs no longer depend on mixed v4 journal row counts.
  The original v4 recording schema/readers are unchanged.
* W2 effect boundaries are split without reordering the original book/trade/
  prepared/gap/context/closed-position calls. In particular book retains
  cross -> maker -> label -> maker-post, and trade retains maker -> label ->
  cross. The default all-on result and clock-call order match the exact
  `23d3525` combined observer, retained as a test reference. Plan construction,
  freshness timing and geometry are unchanged.
* `ml/probe.py` owns the extracted shadow actor, startup waiting and 5ms
  heartbeat. Regression against the exact script class preserves the 10s grid,
  queue=32, duplicate-grid behavior, strict expiry boundary, submission population
  and outputs. Native request/full/expired/shutdown paths carry ownership. Worker
  parent and relay clocks can be recorded. The ordinary engine imports no ML.
* CrossVenue task creation is sorted; instrument/raw/update/gap events have
  explicit native boundaries. Removed watch tasks remain owned until cleanup is
  joined. Missing external data still has no Bybit execution authority.
* `NativeChildReplayBridge` runs the actual spawned `InferenceWorker`, real
  queues and reply relay. Child/relay consume their recorded boundaries and
  clock values through an async coordinator. The parent does not wait on a
  child clock or take a relay mutex while the relay waits on that coordinator.
  Actual forecast probabilities and adapter outputs are compared. Model and
  manifest hashes are checked before/after replay; missing provenance fails.
  Processes/threads are joined and their receipt is retained. There is no local
  Predictor recomputation fallback in the fixture's replay path.
* `IndexedNativeTape` validates the full gzip/hash/dispatch/task/scope/footer
  population into SQLite, including task validation state. It has no fixture
  row cap or sample/truncation path. Supplied external raw inventories and exact
  source/model/probe artifacts are digest-checked. It always denies production
  coverage until the missing execution proof exists. The fixture reader's fail
  cap remains intact; an existing index is never trusted or overwritten.

## Two distinct offline witnesses

The new runtime regression executes the real service loops, bootstrap source
gather, symbol worker, dual Bybit streams, receive/process queues, W2 book/gap
hooks and event evaluation with a real terminal callback. Only the public source
adapters are offline. All declared tasks terminate and both source messages are
enqueued/dequeued exactly. Its flat source has **no ordinary trade or full label**;
it is a recording integration test, not the P8 complete acceptance population.

The source-frozen operation witness now has **284 tokens: 25 tasks, 145 clocks,
64 boundaries**, with all six variants repeated twice. F uses the genuine frozen
V2 model in a real child during capture **and replay**. All tokens are consumed
or explicitly excluded by module, no enabled leftovers, and repeated ordinary
operation hashes agree. Four actual child replay joins are present in the two F
reports. Its engine scheduler remains disabled and its portfolio/label population
remains the older empty-portfolio/rejection fixture. These two witnesses must not
be combined into a claim of a single complete production replay.

Final model-bound population:
`8e027832c166052f551dcfaadcc85b6fe5e00ccb01dc5051bcfa38785a7d4049`.
Exact source/config/runtime/schema/model hashes, all reports, indexed validation,
prior attempts and raw inventories are in
[`docs/wave2-evidence/n3-production-v5/`](wave2-evidence/n3-production-v5/).
Raw files are retained at the inventory's local paths; they were not upgraded or
edited to fill missing observations. Original historical captures are unchanged.

## Exact remaining blocker

The explicit-operation driver does not execute arbitrary `runtime` coroutine
roots or replay the entire probe heartbeat/startup/poll/timeout population.
The child bridge requires a granted parent synchronous slice. If a different
actor's token interrupts that slice, it fails at the first token rather than
blocking the event loop, stealing clocks or draining tokens anonymously.
Complete producer startup/shutdown lifetime proof and source-adapter replay
still need to be joined to those runtime slices.

P8 is therefore not satisfied: there is no single capture/replay population
containing native services, nonempty ordinary decisions, EconomicPlan/FIRE,
PaperBroker fill/managed close, complete first-prepared executable label,
split hooks and the full real V2 actor lifecycle. Consequently a valid production
N3.0 A-F cost report cannot be produced. Callback residence includes replay
coordination and per-request child startup; subtracting it would misattribute
cost. No p99 components were summed and no bottleneck was selected from it.

## Gates

| Gate | Status | Boundary |
|---|---|---|
| Pre-code exact-head audit/plan | MET | Separate preceding commit |
| Native recording roots/clock contracts | PARTIAL | Named runtime paths tested; no whole-production coverage receipt |
| All-on W2 split parity | MET | Exact baseline outputs and clock order on regression population |
| Frozen probe extraction parity | MET | Grid/queue/expiry/submission regression; whole actor replay missing |
| CrossVenue lifecycle integration | PARTIAL | Deterministic ownership and join fix; complete external session proof absent |
| Real child request replay | MET, scoped | Actual frozen model, IPC/relay, exact output and join receipts |
| Whole worker/probe/runtime replay | NOT_MET | Parent slice coordinator and full control/lifetime population missing |
| Disk-backed structural validation | MET, scoped | Complete supplied tape; production coverage remains false |
| P8 complete acceptance population | NOT_MET | No unified nonempty execution + executable-label + actor replay |
| Production v5 coverage / N3.0 | NOT_MET | No automatic complete-session production proof |
| Eligible production A-F cost / tax | NOT_TESTED | Operation fixture timing is ineligible |
| Profile-directed latency fix | NOT_TESTED | Blocked by P9 |
| Controlled W2.0 | NOT_MET | No eligible latency/semantic acceptance |
| One 900s public-paper attempt | NOT_TESTED | Not started |
| Natural executable-label parity | INCONCLUSIVE | No new natural setup |
| Dataset | NOT_MET | `wave2-population-20260928-v1` unchanged |
| Maker | NO_SUPPORTED_HYPOTHESIS_YET | No execution/ML |
| CrossVenue | telemetry-only | No trading influence |

Latest actual native loop/data-to-adapter p99 remains **24.4621 / 308.2433ms**
against budgets **20 / 250ms**. The historical 272.593ms coarse span remains
historical; the new request witness cannot reconstruct its missing boundaries.
No training, paired market, applied riskScale, maker execution, Demo/mainnet,
900s, retry/extension, 8h/12h or merge was performed.

## Defect receipts and validation

The pre-existing CrossVenue retired-task join defect has a failing-before receipt
and passing regression. During this patch, tests also caught a relay queue-mutex
deadlock, missing event completion-callback ownership, and terminal cleanup
masking the first native error. Each failing attempt is retained alongside the
passing-after receipt. The first full preflight found one `__new__` profiler
compatibility regression (1718 passed / 1 failed); it was fixed without weakening
the existing test. New feature absence is not presented as a historical defect.

Final full Windows preflight: **1723 passed in 231.73s**.
Expanded Windows ML/spawn/codec/CrossVenue/paired/native suite:
**622 passed in 74.72s**. Both JS syntax checks pass.
The earlier clean preflight (1721) and expanded run (621) are retained separately.
Exact-head Linux/Windows CI is recorded in the final external receipt and PR61;
it runs on the final evidence commit. Green tests do not close P8/P9, W2.0,
natural labels or profitability.
