# Current continuation from exact 23d3525

Current authority: [production wiring and child replay evidence](n3-production-v5-evidence.md).
Native production recording roots, W2 split/probe extraction and real spawned
request replay are now implemented and regression-tested. The operation witness
is 284 tokens, twice A–F, with actual child/relay replay in F. A disk-backed
validator checks complete supplied tapes, but cannot grant production coverage.

**Complete production N3.0 and W2.0 remain NOT_MET.** Full runtime/probe
coroutine replay and a unified nonempty P8 execution/label population are absent.
No eligible production cost/tax or latency fix, and no market attempt. The
280-token local-recompute witness below is retained as historical evidence.

---

# N3 prospective v5 architecture evidence — 28 September 2026

**W2.0 remains NOT_MET. Complete production N3.0 is not implemented.**
This patch establishes an executable prospective v5 contract and an explicit-
operation native asyncio driver, with a production-component offline witness.
It does not claim complete native engine/IPC replay, eligible module-cost A-F,
natural labels or a latency improvement.

Baseline PR61: `e4de7a0fbebf308b3529dc89dc7d4ebeace5bebf`, stacked on unchanged
PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Audit/plan was committed before
code: local `047c0a3`, published `74c57d5954a3ec09635c49fda1ca86c76c8db518`;
identical tree `36946ad64cb0657142b414bc2932853a7adcc538`.
Implementation: local `cc309d3`, published
`f925838392ccd4aa17855e44aaa02b96e18baa63`, identical tree
`20400892c1a1e5fa5731cc8a449f1934625c7a3d`.

## Implemented and bounded

* `native_v5.py`: opt-in `native-causal-v5` stream. Parent, spawned inference
  worker and reply relay reserve an explicit sequence in a common fixed shared
  byte ring at observation time. Clock reads occur under that publication lock.
  No ordering by timestamps, delayed parent token assignment or old-tape upgrade.
* Every event carries module/task/parent/producer identity and declared source
  correlation. Strict lifecycle validation rejects unknown ownership, duplicate
  tokens, invalid external enqueue/dequeue/dispatch, missing V2 request/reply/
  relay stages, post-terminal events and unfinished tasks. Failure terminals are
  retained. The bounded lock fails on abandoned ownership rather than hanging.
* JSON/hash/gzip runs on the drain thread. The ring retains its byte charge until
  the batch is written. Overflow, serialization errors and writer errors poison
  the capture. No sampling, queue-limit increase or GC change. Old v4 input
  schema, replay code, captures and manifests are unchanged.
* Canonical SHA256 chain, versioned header/footer and exact frozen source/config/
  runtime/schema hashes are validated before execution. The header explicitly
  denies production completeness. The in-memory reader has an explicit fail cap.
* `InferenceWorker` has an optional shared endpoint. It records actual worker
  receive/start, prediction completion, reply feeder enqueue/receive, relay
  enqueue/dequeue and adapter receipt; the caller completes the adapter result.
  Default worker use remains compatible. Poisoned recording still joins worker
  and relay. Existing queue capacities, TTL and prediction policy are unchanged.
* `NativeControlledDriver` runs registered callbacks as native asyncio tasks,
  routes clocks by task/module and checks computed outputs. Six cumulative
  projections consume one source tape. Disabled owned tokens are explicitly
  accounted for, and enabled dependencies on disabled parents fail. No anonymous
  clock drains, leftover warnings or logical-wall-time latency substitution.

## Offline proof and its limits

The fixture runs real TradingEngine market/evaluate callbacks, SegmentRegistry,
CrossVenue instrument parsing/gaps/events, MakerShadow, a PreparedLabelEngine
rejection, and the frozen existing V2 Predictor in the real spawn worker/relay.
No network calls, model fit, credentials or orders. Synthetic feature vectors
are identified as synthetic; they are not natural market forecasts.

A-F replay twice on one tape checks exact operation outputs and ordinary fixture
state. In this witness the event-driven engine scheduler is disabled, live
services are absent, the portfolio is empty, and the V3 path is an economics
rejection. V2 replay recomputes predictions locally, not in the worker. Thus its
operation hash is **not** a complete-session ordinary decision/portfolio proof.
Reports keep those production hashes null and `controlledW20=NOT_MET`.

Initial complete witness: **25 tasks / 141 clocks / 64 boundaries**, all 280
tokens accounted for, exact repeatability, same ordinary operation hash across
all variants. Updated exact-source witness and inventories are retained in the
evidence directory. Callback replay residence includes coordinator waiting and
must not be subtracted to claim production incremental module cost.

Final exact-source population:
`d7577b8847d2a10e09e614663450b62ee0f1368c468235508eb3846c95b9ddb8`.
All twelve variant/repeat reports have ordinary operation hash
`e4c6d4ddd364b8e993883acfad09405827c32d9142cbbe2cb4cd55c04a4a136d`.
Writer: 280 accepted/written, zero rejected, 142,540-byte high-water / 1MiB.

Two real implementation defects were caught red-before/green-after: a rejected
non-JSON observation initially did not poison the tape, and a drained batch
initially released its byte budget before writer acknowledgment. Both receipts
are preserved. The earlier missing-module collection failure is a feature
absence receipt, not presented as a discovered production regression.

## Instrumentation tax

Fixed ABBA protocol, 160 batches of 64 clocks at 10ms intervals (6400 reads/s),
all observations retained, GC enabled, 1MiB ring. Initial run measured about
**4.943 microseconds additional callback time per recorded clock**, with zero
rejections and **11,392 bytes** maximum high-water. Both recording arms preserved
all 10,240 clocks and complete chains. Final exact-source measurement is retained
separately; these small synthetic timings are not W2 acceptance or whole-engine
overhead. True allocation throughput/top sites remain NOT_TESTED.

Final source-frozen ABBA: **4.860 microseconds/clock** additional callback time,
recording loop p99 **14.7762 / 15.0187ms**, maximum high-water **11,482 bytes**,
zero rejects, all 20,480 recorded clock observations validated. Baseline loop p99
was 15.4117 / 14.9604ms; no claim that recording improves latency. This protocol
is an overhead/throughput witness only and has no data-to-adapter population.

Real IPC critical paths use joined boundaries of each individual request. Queue
feeder enqueue to worker receive includes queue/feeder/scheduling, and reply
enqueue to receive includes analogous work. These are not isolated kernel wire
time. No marginal percentile sums or extrapolation to the old native p99 item.

## Gates

| Gate | Status | Scope / blocker |
|---|---|---|
| File audit and pre-code plan | MET | Exact baseline; plan committed first |
| v5 explicit-owned contract/chain/lifecycle | MET | Opt-in bounded fixture contract, not full production coverage |
| Production v5 capture coverage | NOT_MET | Native service/transport/probe roots and module split not integrated |
| Bounded recorder instrumentation proof | MET | Fixed synthetic protocol only; no complete-engine tax claim |
| Explicit-operation driver fixture A-F | MET | Two repeats, zero leftovers, exact computed fixture outputs |
| N3.0 complete production native executor | NOT_MET | Native async root/worker replay bridge and full observer partition absent |
| Eligible production A-F module cost | NOT_TESTED | Fixture residence is not production module cost |
| Profile-directed latency fix | NOT_TESTED | No eligible attribution; no speculative perf patch |
| Controlled W2.0 (H) | NOT_MET | No eligible complete production acceptance population |
| One 900s v5 public-paper attempt | NOT_TESTED | Not authorized by current H; not started |
| Native executable label parity | INCONCLUSIVE | Fixture rejection is not a natural frozen-label path |
| Independent dataset | NOT_MET | `wave2-population-20260928-v1` unchanged |
| Maker | NOT_TESTED | `NO_SUPPORTED_HYPOTHESIS_YET` unchanged |
| CrossVenue influence | INCONCLUSIVE | Telemetry only |
| V3 training / paired / Demo / 8h / 12h | NOT_TESTED | All downstream execution remains frozen |

Latest actual native loop/data-to-adapter p99 remains **24.4621 / 308.2433ms**.
The old p99 item's 272.5930ms features-ready to prediction-end interval still
has no recoverable fine native boundaries. No old raw/manifests were modified.

The [production architecture boundary and minimal next patch](n3-native-v5-architecture.md)
identifies each required owner, change and acceptance test. Adding more fields
or sorting old timestamps cannot bridge it. This is the stopping boundary for
this architecture patch; it does not complete the originally requested production
executor or justify moving to F/G/H/I.

## Validation and provenance

Full Windows preflight: **1705 passed in 235.33s**. Expanded Windows
ML/spawn/codec/CrossVenue/paired/native suites: **604 passed in 72.60s**.
Both JS syntax checks pass. The first full attempt is retained: 1701 passed /
4 failed. The offline fixture was moved into the ML package to respect the
existing import boundary; three subprocess v4 replay failures were local source
selection errors, resolved by setting PYTHONPATH to this exact checkout instead
of its older editable installation. The 142 focused checks then passed, followed
by the full clean run. No v4 validation or test expectation was weakened.

Implementation CI: [36422588587](https://github.com/branch-danya-dev/scalp-bot/actions/runs/36422588587).
Final exact-head status is recorded in PR61 and the local final CI receipt.

Receipts, source/model/config/runtime/schema freezes, raw inventories, fixture
reports, tax reports and failed attempts are retained under
`docs/wave2-evidence/n3-native-v5/`. Final test counts and exact-head CI are
recorded in `validation-summary.json` and the PR. Green tests do not close any
production gate above.
