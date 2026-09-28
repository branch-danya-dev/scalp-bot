# Prospective native v5 audit and implementation plan

Audit baseline: PR61 `e4de7a0fbebf308b3529dc89dc7d4ebeace5bebf`;
PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. This document is committed
before implementation. `docs/n3-native-evidence.md` remains authoritative for
the retained v4 captures. No reinterpretation, migration or mutation of them.

## File-level audit

| File / entry point | Existing contract and missing boundary |
|---|---|
| `runtime_clock.py:RecordingRuntimeClock` | Observes method/value, no module/task owner; `ClockTape` consumes a single anonymous sequence. |
| `engine.py:TradingEngine.__init__`, `_record_clock_read` | Wraps engine/broker clock after capture manifest; session clocks inherit it. Recorder retains unwrapped clock to prevent recursion. |
| `engine.py:_input_sleep`, `_record_scheduler`, `_launch_event_evaluation` | Records ordinary wait/wake, schedule/start/coalesce/sleep/resume/finish. Does not encompass V2 heartbeat or external venue tasks. |
| `engine.py:_launch_service_tasks`, `_launch_symbol_worker`, `_launch_run_timer` | Native asyncio task roots; context/scanner/clock/arbiter/source await boundaries must retain identities, cancellation and terminal ownership. |
| `engine.py:_market_handler`, `_evaluate`, `market_runtime.py`, `scenario_runtime.py` | Ordinary time-sensitive decisions and sessions; W2 hooks call the same clock inside these scopes. Changing clock consumption changes replay branches. |
| `input_scope.py:InputScopes.enter`, `source_await.py` | Scope parents use ContextVar; scopes are not native tasks. A child task may inherit a parent scope after the parent has returned. |
| `input_journal.py:InputJournal`, `DeferredClockRow` | Explicit v1-v4 validation; sequences assigned before enqueue, deferred scalar clocks, writer-owned canonical chain. No cross-process total-order allocator. |
| `capture.py:InputWriter`, `_BatchQueue` | 32MiB/32768 bounded queue; 1024-row / 4MiB ready batches, one codec request; rejection invalidates capture. Do not increase limits. |
| `capture_codec.py:CaptureCodec`, `_worker` | Real spawn process; parent owns deferred chain and applies child receipts. Codec diagnostics are separate from causal strategy clocks. |
| `offline_segment.py:_Cursor`, `_bindings` | Strict anonymous clock cursor, first divergence retained. Requires unchanged scopes and method order. |
| `offline_scheduler.py:OfflineScheduledReplay.apply` | Manually steps coroutines using recorded ordinary scopes/waits. Neither OS loop lateness nor real IPC replay; no W2 dispatcher. |
| `capture_replay.py:IndexedInputs`, `offline_bootstrap.py` | Indexed primary stream and exact cold source/config/runtime binding. Cannot admit a new format as v4. |
| `cross_venue_public.py:PublicCrossVenueService` | Instrument REST results, gap before connect, raw/decode/ingest, disconnect/cancel and unordered set-based watch task creation. All lack a common primary total-order token. |
| `ml/wave2.py:Wave2Observer` | `book`/`trade`/`gap` combine CrossVenue, Maker and label calls; `prepared` combines risk-plan, Segment assessment, CrossVenue snapshot and labels. No independent B/C/D/E dispatch surfaces. |
| `ml/prepared_dataset.py`, `scenario_runtime.py:_observe_prepared_intent` | Prepared identity/feature collection receives engine/session clocks. First-prepared frozen-label policy is separate from ordinary execution economics. |
| `scripts/smoke-trading-model.py:ShadowProbe` | Monkey-patches journal and evaluate, owns 32-item queue, 10s feature grid and independent polling heartbeat; queue expiry/full and shutdown need explicit request terminal evidence. |
| `ml/worker.py:InferenceWorker` | Spawn worker, 12-item latest queue, one in-flight request, 1-item IPC queues and relay queue. Trace is emitted only after completion; worker receive/start, send/receive and poll order not globally sequenced. |
| `ml/worker.py:_inference_main`, `_receive_replies`, `poll` | OS clock reads in process/thread. Retrospective parent assignment cannot recover their original relative dispatch order. `multiprocessing.Queue.put` is feeder enqueue, not wire-send completion. |
| `ml/shadow.py:ShadowAdapter.accept` | Read-only proposal path uses caller-supplied observation; must preserve prediction values, freshness policy and refusal reasons. |
| `bybit.py:_stream_topics` / processor | Receive/parse/enqueue/dequeue and callback processing have independent native tasks and clocks; ordinary primary capture begins at handler. Native ingress has to be admitted explicitly. |
| `recorder.py`, `pipeline_evidence.py` | Recorder/GC diagnostic clocks are not trading clocks. Retained-block deltas are not allocation throughput. |

## Architectural decision

A new, explicit `native-causal-v5` stream is an opt-in contract, not a renamed v4
input journal. Old validators and captures keep their byte/semantic contract.
Capture eligibility is distinct from contract-fixture validity. A fixture must
never grant market authorization or close W2.0.

The minimal required architecture is a common sequencer shared by parent,
worker and relay, owner-bound clocks, explicit tasks and source correlation,
and dispatcher-owned runnable operations. A token is reserved at observation,
not assigned when a delayed trace reaches the parent. Ordering is by token,
never by wall time. IPC operations distinguish queue submission from actual
worker receive; no claim of separately measured kernel wire time.

Use a fixed-size shared ring with bounded payloads and explicit overflow failure;
hash/JSON/gzip work belongs to the draining writer. Queue memory stays at or
below the existing 32MiB capture budget. No sampling, GC disablement, increased
limits or unbounded pending trace/reorder collection. Header/footer bind exact
source/config/runtime/schema, total count and final canonical chain.

Owner records carry module, task/parent, producer, source identity and sequence.
Tasks must have one declared parent and a terminal. Async operation transitions
must be explicit and validated. Clock values retain types and call methods;
disabled modules are excluded as named owned tasks, with audited coverage,
never consumed as anonymous no-op clocks. Cross-module dependencies cannot be
silently dropped. Unknown modules, transitions, schema versions and leftovers fail.

## Implementation and proof sequence

1. Commit this audit/plan, then add regressions which fail before v5 exists.
2. Implement the bounded prospective sequencer, versioned immutable tape,
   structural/lifecycle/chain validator, source provenance and strict owner clock
   cursor. Prove equal timestamps, cross-thread/process ordering and failure paths.
3. Implement a separate `NativeControlledDriver` for explicitly registered native
   callbacks and recorded module tasks. Exercise native asyncio, real spawn IPC
   and relay. Keep semantic replay, controlled callback cost and original native
   end-to-end timings in separate fields. Unknown/unbound production paths fail.
4. Build a self-contained offline fixture with actual production components where
   possible: market/core, SegmentRegistry, CrossVenue, Maker, V3 label input and V2
   worker/relay. Prove 100% task/dispatch/clock accounting, hashes and repeats.
5. Integrate production only at explicit owner/dispatcher boundaries. Do not
   substitute a toy callback, manually stepped coroutine or supplied output hash
   for ordinary TradingEngine execution. If complete production integration needs
   an additional architecture patch, retain executable boundary tests and document
   the precise unsupported callsites and minimal patch before any performance claim.
6. Measure instrumentation tax on a fixed synthetic workload, including failures,
   raw inventory and writer high-water. Only eligible complete production A-F
   populations can be used for F/G/H; fixture timings cannot close H.
7. If F is eligible, join per-item spans and select a measured bottleneck; add a
   failing work-bound test before a semantics-preserving patch. Otherwise do not
   optimize speculatively and leave F/G/H explicitly unqualified.
8. Run full Windows preflight, expanded spawn/codec/CrossVenue/native suites,
   JS syntax, and exact-head Linux/Windows CI. Commit evidence and update PR61
   while preserving draft, main and PR60.

## Acceptance / stops

Controlled H requires eligible complete A-F population, loop p99 <=20ms,
data-to-adapter p99 <=250ms, zero losses/rejects/backpressure, full scope/task/
clock/dispatch/chain validation, no leftovers and exact applicable ordinary
decision/portfolio/admission equivalence. Numeric fixture success is insufficient.

Only H=MET permits one pre-frozen 900s v5 market attempt, without retry/extension.
Natural labels additionally require exact same-contract native replay. Dataset
policy `wave2-population-20260928-v1` stays frozen. No training, applied registry
riskScale, CrossVenue influence, maker execution/ML, paired market, Demo orders,
mainnet or 8h/12h. Maker remains `NO_SUPPORTED_HYPOTHESIS_YET`.
