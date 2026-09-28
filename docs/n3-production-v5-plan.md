# N3 production integration audit and pre-code plan

Baseline: exact draft PR61 `23d35252224f1dd472aaee8282b82e7796e3edf2`;
base PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Neither main nor
PR60 is a write target. This plan is committed before implementation.

Authority: n3-native-v5-evidence.md, n3-native-v5-architecture.md,
HANDOFF.md, wave2-roadmap.md and the attached final-head evidence. The latter
adds the published head and final CI receipt, without changing gate status.
The 280-token witness is an explicit-operation proof only. W2.0 and complete
production N3.0 remain NOT_MET. No market or downstream execution is authorized
by the current evidence.

## File-level audit at the exact baseline

| Owner | Observed boundary | Planned change / proof |
|---|---|---|
| input_journal.py; scenario_runtime.py:155; ml/wave2.py:21; smoke-trading-model.py | source identity uses journal sequence including clocks | immutable independent ingress identity, retained across projections; legacy v4 remains separate |
| native_v5.py | fixed shared ring, explicit tasks, per-request worker boundary validation; fixture-only header | extend versioned ownership contract without weakening existing fixture checks; streaming complete-session validation |
| native_controlled.py | registered operation callbacks; synchronous clock reads cannot yield across actors | production task/await dispatcher and real child/relay bridge; explicit disabled-module exclusions and hard leftovers |
| engine.py:1161,1200,1370,1739,1932,2298; source_await.py; input_scope.py | asyncio roots and source/sleep/context awaits lack v5 ownership | task registry, create/start/resume/cancel/terminal tokens; task ContextVar, scopes and shutdown join audit |
| bybit.py:770,872,890 | receive/process tasks, enqueue/dequeue and reconnect run outside v5 | explicit ingress and transport ownership, immutable source association, equal-clock dispatch test |
| runtime_clock.py; engine/session/broker clock construction | engine uses a common v4 clock wrapper | context-routed clocks; unknown owners fail closed; diagnostic/codec time stays outside runtime semantics |
| ml/wave2.py | combined hooks interleave cross/maker/labels and segment assessment | split at actual effect boundaries, preserving existing order, shared input values and clock placement; old all-on output parity |
| scripts/smoke-trading-model.py:16 | frozen probe lives in script, 10s grid and queue=32 | extract actor; preserve cadence/TTL/epoch/refusal policy; record all startup/poll/deadline/terminal paths |
| cross_venue_public.py | unordered set iteration for tasks; cancelled removed tasks are not joined by close | deterministic task lifecycle, instrument/raw/update/gap/cancel dispatch; all retired tasks joined; telemetry only |
| ml/worker.py | genuine capture child/relay stages; parent polling/startup uses OS time, child replay absent | owned worker/control clocks and actor bridge driving real spawn; computed prediction/model/source equality; joins before footer |
| ml/native_fixture.py | scheduler disabled, empty portfolio, V3 rejection, local prediction replay | separate production-like full population: native scheduling, plan/FIRE/fill/managed close, executable label, real child |
| native_qualification.py; pipeline_evidence.py; scripts/check-native-v5.py | historical timing and fixture gates are deliberately limited | true cumulative A-F reports, per-item joins, tax and strict coverage receipts; never sum marginal percentiles |
| tests; .github/workflows/strategy-rework-checks.yml | existing full/expanded suites and JS checks | targeted red/green receipts, full Windows preflight, expanded spawn/codec/CrossVenue/native CI, exact final-head Linux CI |

## Dependency and acceptance sequence

1. P1/P2: ingress identity, production NativeDispatch ownership and routed clock
   context. Prove cancellation before first execution, missing/duplicate owner,
   terminal and scope failures, exact clock/source semantics across projections.
2. P3/P4/P5: split observer, extract frozen probe, bind external service ingress.
   Preserve all-on outputs and source clock ordering. Keep ordinary execution
   independent of optional ML and external-venue availability.
3. P6/P7: actual spawned child/relay replay and disk-backed full tape adapter.
   Validate full chain/scopes/tasks/dispatch/footer/external inventory and exact
   source/config/runtime/schema/model/probe hashes. No local Predictor fallback.
4. P8/P9: production-like full acceptance population before any market; repeat
   all six variants on the same source population, with nonempty ordinary
   portfolio and executable label. Require exact decisions/admission/execution/
   portfolio parity, 100% tokens, explicit owned exclusions and no leftovers.
   Measure joined pipeline spans, callback/writer/GC/allocation evidence and
   instrumentation tax on that population. Fixture timing alone is ineligible.
5. P10 only after eligible P9: select a measured bottleneck, record a failing
   work-bound regression, fix only that cause, prove semantic parity and repeat
   controlled performance. No resource-limit increases, cadence changes, GC
   disabling, model/feature/risk/economics changes or reduced observations.
6. P11 only after controlled W2.0 MET: exactly one preregistered 900s public-paper
   attempt with pre-Start freezes; no retry/extension. P12 requires exact replay
   of a natural frozen executable-label contract if a natural setup exists.

Controlled acceptance is loop p99 <=20ms AND data-to-adapter p99 <=250ms,
zero rejects/loss/backpressure/critical drops, complete validation, no leftovers
and exact ordinary parity. An unmet gate stops all dependent execution and is
reported precisely. Dataset policy wave2-population-20260928-v1, Maker
NO_SUPPORTED_HYPOTHESIS_YET and CrossVenue telemetry-only remain unchanged.

Implementation commits and evidence/docs commits are separate. Preserve failed
attempts, raw inventories, red/green receipts and exact provenance. Green tests
do not establish production coverage, W2.0, natural labels or profitability.
