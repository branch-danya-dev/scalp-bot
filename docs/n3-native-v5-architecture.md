# Current integration state after production wiring

See [current evidence](n3-production-v5-evidence.md) and
[pre-code file audit](n3-production-v5-plan.md).

Implemented: independent source-lane ingress counters and applied-source
references; NativeDispatch coroutine slices and done callbacks; routed engine,
session, broker and transport clocks; separated W2 effects; extracted probe;
CrossVenue task ownership/retired-task joining; real child request replay; and
disk-backed whole-tape structural validation. The original framework remains
fail closed and no productionCoverage override was added.

Remaining architecture boundary: NativeControlledDriver still executes explicit
operations. It does not reconstruct arbitrary runtime coroutine roots/source
await results/probe control cadence. NativeChildReplayBridge consumes real
child/relay clocks asynchronously but rejects a parent synchronous slice that
crosses another actor's token. A whole-runtime resume coordinator, complete
producer lifetimes and the unified nonempty P8/P9 acceptance population are
required before production cost attribution or controlled W2.0 can be claimed.

The two successful witnesses (native-root recording and child-request replay)
must not be aggregated into a fictitious complete-session semantic proof.
Historical architecture below explains the starting point; several listed
component absences are now implemented, while the production gate remains open.

---

# Production integration boundary after the v5 architecture witness

The opt-in sequencer and explicit-operation driver are executable. They are
**not** a replacement for the complete live engine scheduler. The header says
`coverage=explicit_owned_tasks_only`, `productionCoverage=false`; neither the
fixture nor its low loop latency can authorize a market capture.

## Why simply enabling the recorder is insufficient

1. The current primary input identity uses `InputJournal.sequence`, which also
   numbers clock reads. Disabling research hooks removes clock reads and changes
   downstream `source_sequence` values. An independent, frozen ingress identity
   is required before variant projections, not renumbering an old capture.
2. `Wave2Observer.book/trade/prepared/gap/context` combines modules in one call.
   Labelling the whole call `maker` or `v3` misattributes CrossVenue/Segment work;
   labelling it `core` hides incremental cost. Nulling methods discards required
   observation clocks and can alter the first-prepared source reference.
3. Native async roots in `TradingEngine` and `bybit._stream_topics` are created
   directly with asyncio. V2 probe lives in a script wrapper and has its own
   heartbeat. PublicCrossVenueService creates instrument/gap/update tasks outside
   the primary journal. The new ring cannot infer these task ownership transfers.
4. Clock reads inside a synchronous engine slice cannot suspend the parent loop
   waiting for another process's token. `runtime_clock()` deliberately fails at
   such a boundary. A dispatcher must know which operations can yield and resume.
5. Worker recording now has genuine shared tokens for request/worker/reply/relay
   boundaries, but no shared replay coordinator drives the child. Local Predictor
   recomputation proves only fixture outputs. It cannot be reported as replay of
   native worker scheduling or IPC timing. Queue.put records feeder enqueue entry;
   it does not expose kernel send completion or isolate wire time.
6. `read_native_tape` is a bounded in-memory fixture reader, not the disk-backed
   complete-session adapter. Exceeding the explicit event cap raises; nothing is
   truncated, sampled or labelled as a full capture.

## Minimal next architecture patch, in dependency order

| Change | Exact owners | Required proof before activation |
|---|---|---|
| Add ingress identity and NativeDispatch registry | `bybit.py` receive/process enqueue/dequeue; `engine.py` `_launch_service_tasks`, `_launch_symbol_worker`, `_launch_event_evaluation`, `_input_sleep`; `source_await.py` | One declaration per task, recorded cancellation before first execution, parent/owner consistency, explicit resume order including equal timestamps, immutable source IDs independent of disabled hooks |
| Route time with module/task context | `runtime_clock.py`, `input_scope.py`, engine/broker/session clock construction | ContextVar for parent async tasks, explicit serialized context for child/relay; unknown context fails; core observations stay exactly identical A-F; collector/codec/heartbeat diagnostic time kept separate |
| Split observer operations without reordering effects | `ml/wave2.py` `book`, `trade`, `prepared`, `gap`, `closed_position`, `context` | Independent Segment/CrossVenue/Maker/V3 call boundaries in current order, retained shared inputs, exact frozen plans/features and old all-on observer outputs. Do not move risk-plan or freshness reads into a different timing policy |
| Extract frozen ShadowProbe into a runtime actor | `scripts/smoke-trading-model.py`, new `ml/probe.py`; use current optional `InferenceWorker.native_endpoint` | Preserve 10s feature grid, 32-item queue, heartbeat cadence, epoch handling, TTL, probabilities and adapter refusals; record rejected/full/expired/shutdown requests, startup and every poll/deadline clock |
| Bind external ingress to NativeDispatch | `cross_venue_public.py` `start`, `watch`, `_stream`, `close` | Deterministic requested task creation order; instrument responses, reconnect gaps and cancellation dispatch all have tokens even without exchange timestamps; zero source drops |
| Add a native replay actor bridge | `native_controlled.py`, worker child/relay, engine service adapters | Child/relay consume their own clocks and dispatch tokens under recorded scheduler; parent never blocks its event loop on a child clock; actual prediction and adapter outputs are checked; all process/thread terminals joined before tape close |
| Add indexed v5 reader and production coverage receipt | `capture_replay.py` or a versioned native adapter; capture manifest | Streaming full chain, task, scope and dispatch validation; footer and external raw inventory digest, no truncation; exact source/config/runtime/model/probe/schema checks before any execution |

These are production architecture changes, not additional fields on a JSON row.
The present patch supplies the sequencer, lifecycle validator, worker boundary
integration and strict explicit-operation executor on which they can be built.
The API leaves no `productionCoverage=true` escape hatch. A versioned production
adapter and proof are required before that coverage can be declared.

## Required acceptance population

Extend the fixture to run native service/source/event scheduling, all split W2
operations and the real child replay actor on one complete prospective tape.
Include nonempty ordinary decisions/admissions and managed portfolio transitions,
plus full first-prepared label lifecycle, not only empty positions or a rejection.
Retain exact core hashes with each cumulative module, per-item queue/IPC/relay
spans and named exclusions. A disabled module's clocks are explicitly owned
exclusions; a remaining enabled token is an error.

Only this population can support production B-A/C-B/D-C/E-D/F-E cost, a causal
bottleneck hypothesis and a semantics-preserving work-bound regression. Until
then F/G/H and the 900s gate remain unqualified. No performance patch was chosen
from the fixture and the old 24.4621/308.2433ms native evidence is unchanged.
