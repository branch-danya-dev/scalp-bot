# Whole-runtime replay: exact-head audit and pre-code plan

Baseline PR61: `b3027ede153d657981657acc7ddf148ecb564918`, tree
`59553aa43cecd4395d67b7fd0d902796769acc04`. PR60 remains
`1a2d0f67da0331df7d2d134bffde6f64141c7898`; main and PR60 are not write
targets. GitHub metadata and the fetched commit were checked before this plan.
The clean previous checkout is reused on a new continuation branch.

Authority: `n3-production-v5-evidence.md`, `HANDOFF.md`, `wave2-roadmap.md`.
The previous output copy is available locally; the referenced `/mnt/data` upload
is not present in this Windows workspace. Existing evidence explicitly leaves
production N3.0 and W2.0 NOT_MET. Two partial witnesses are not unified proof.

## Exact baseline file audit and actor ownership

| Files / roots | Current contract and required replay behavior |
|---|---|
| `native_dispatch.py:NativeDispatch` | Actual coroutine iterator records create/start/resume/suspend/end. Runtime IDs use per-module serials, parent is the current owned scope, producer is parent-asyncio. Inherited unregistered tasks cannot read clocks. Reuse these records and ownership checks, including cancellation before start and done callbacks. |
| `native_controlled.py:NativeControlledDriver` | Registered-operation executor only; synchronous clock cannot yield. Reuse validated A-F projection and accounting contract; add a distinct runtime coordinator executing the actual roots, never replay supplied outputs as results. |
| `native_v5.py`, `native_index.py` | Sequencer publishes under one shared lock; timestamps do not schedule. Indexed reader validates full population. Preserve schema compatibility checks, chain/footer and hard lifecycle validation; prospective contract changes require a new schema hash, never rewriting old raw. |
| `engine.py:_native_gather/_launch_service_tasks/_launch_symbol_worker` | Bootstrap source children; scanner/context/arbiter/clock roots; market symbol workers. Every child creation must be executed by its recorded parent, every resume by its recorded task/module. Source results must come from a frozen source adapter with independent ingress identity. |
| `engine.py:_launch_event_evaluation/_track_event_task` | Event root and separately declared completion callback. Callback must run only after its real task terminates; preserve event owner identity and ordinary decision order. |
| `engine.py:_input_sleep`, `source_await.py`, `input_scope.py` | Native sleep and source outcome markers surround actual awaits; coroutine slices also cover library awaits. Replay controls readiness at explicit await boundaries, retains exceptions/cancellation and verifies causal scope ordering. |
| `bybit.py:_stream_topics/_receive_or_processor_failure/_consume_market_queue` | Real receive/process and bounded FIFO queue; native enqueue/dequeue and independent source identity. Offline connector returns frozen wire messages through this exact architecture; no direct market callbacks. |
| `ml/probe.py:ShadowProbe` | Startup/start/wait, 5ms heartbeat, poll, 10s feature grid, queue=32, strict TTL, epoch and terminal branches. Scope spans can survive an await. Full actor replay must preserve these scopes and every poll result, not just requests. |
| `ml/worker.py`, `ml/native_bridge.py` | Genuine spawn and relay; request tokens span parent/inference-worker/reply-relay. Current bridge grants only uninterrupted parent slices. Startup readiness and queue availability are nondeterministic external outcomes, currently not explicit complete lifecycle inputs. Add prospective owned control outcomes if needed; never use local Predictor fallback. |
| `cross_venue_public.py:PublicCrossVenueService` | Instrument fetch, sorted watch roots, recv timeout/reconnect/gap/cancel, retired-task joins. Replay source results and exceptions under recorded cross_venue ownership. No Bybit execution authority. |
| `ml/wave2.py`, `scenario_runtime.py`, `admission.py` | Split Segment/Cross/Maker/V3 boundaries, first-prepared observation before ordinary economics/FIRE. Disabled modules exclude only their declared tokens; ordinary event/admission hashes must remain exact. |
| `paper.py`, `ml/prepared_labels.py` | Ordinary fills/management and derivative frozen-plan labels share real execution policy. Never mutate a ledger to manufacture acceptance. Require nonempty position, managed close and complete first-prepared executable label. |
| `engine.py:close/_shutdown_service_tasks`, probe/cross close | Actual cancellation, acknowledgement and join must precede terminal/footer. Unknown owner, parent mismatch, duplicate terminal, impossible resume and enabled leftovers are hard failures. |
| `tests/ml/test_native_runtime_capture.py`, `ml/native_fixture.py` | First is flat production recording; second disables scheduling and has empty portfolio/rejected V3. Neither is eligible. Replace neither historical witness; add one independently frozen unified population. |

## Implementation sequence

1. Commit this audit before code. Add failing-before runtime/inter-actor tests
   with retained receipts. Validate complete session repeats, cancellation,
   nested module scopes, callback order, unknown owners and impossible resumes.
2. Implement `WholeRuntimeReplayCoordinator` around NativeDispatch and the v5
   ownership/projection contracts. Native observations are safe stack suspension
   boundaries: a replay-only stackful adapter can park a synchronous slice,
   yield its outer asyncio task to the coordinator, service the exact next
   producer, and resume the original stack. Evaluate a narrowly scoped optional
   greenlet dependency for this; never block the event loop waiting on child
   clocks, drain another owner's tokens, sort timestamps or add polling loops.
   If the required boundary is unsafe, retain the exact architectural blocker.
3. Replay actual source/runtime/probe lifetimes. Add owned prospective worker
   control observations where readiness or process/queue state changes behavior;
   preserve live 10s grid, queue limits, 5ms cadence, TTL, outputs and refusal.
   Preserve exact all-on probe and observer regression parity. Real spawned child
   and relay must compute outputs and join throughout the full session.
4. Build deterministic source fixtures at REST/WebSocket interfaces, driving
   actual native service/symbol/event/receive/process tasks. Establish one
   nonempty production-like population through real strategy/admission/broker
   and frozen-plan executable-label paths, with all split hooks and full probe.
   Source/config/runtime/schema/probe/model hashes are frozen before capture.
5. R5 precedes R6: two exact full replays, all task/clock/dispatch consumption,
   explicit owned exclusions, no leftovers, exact source/ordinary/label/child
   hashes. Validate full indexed tape and all lifecycle terminals.
6. Only then run cumulative A-F on that one population. Preserve joined per-item
   stage spans, event-loop/data-to-adapter quantiles, queue/callback/IPC/relay/
   adapter/writer/GC metrics and safe allocation measurements. Never add
   independent p99 values. Measure whole-population instrumentation tax separately;
   replay coordination residence is not production cost.
7. Only valid R6 may select a latency fix: failing-before work-bound regression,
   measured fix, full A-F repeat and semantic parity. Do not alter strategy,
   risk/economics/fees/slippage/model/cadence/limits/GC/population.
8. W2.0 requires eligible nonempty proof, loop p99 <=20ms, data-to-adapter p99
   <=250ms and zero loss/rejects/backpressure/drops. Otherwise stop dependent
   stages on a precise blocker. Only MET authorizes one preregistered 900s
   public-paper capture; no retry/extension. No fills means INCONCLUSIVE_NO_FILLS.

## Validation and evidence

Keep implementation and evidence commits separate. Retain all raw/failed
attempts, full freezes, red/green and A-F/tax reports. Run full Windows preflight,
expanded Windows ML/spawn/codec/CrossVenue/paired/native suites, JS syntax and
exact-final-head Linux CI. Update HANDOFF, roadmap, evidence and gate table.
Tests alone never establish production N3.0.

Dataset `wave2-population-20260928-v1`, Maker NO_SUPPORTED_HYPOTHESIS_YET and
telemetry-only CrossVenue are unchanged. No V3 training, paired market, maker
execution/ML, applied riskScale, Demo/mainnet or 8h/12h before the required gates.
