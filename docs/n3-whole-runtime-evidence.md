# Whole-runtime continuation: NOT_MET

**Production N3.0 = NOT_MET; W2.0 = NOT_MET. No market attempt is authorized by
these results.** The whole-runtime coordinator and a unified nonempty offline
population now exist, but the strict final repeat failed. Native A-F incremental
cost and whole-population instrumentation tax have not been established.

Baseline PR61 is `b3027ede153d657981657acc7ddf148ecb564918`; PR60 remains
`1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main remains
`dfcc5949f2b5cb6f902e304dfbe5bd1f4a7b3132`. Neither is a write target.
The [file-level audit and plan](n3-whole-runtime-plan.md) was committed before code.

## Implementation

- `whole_runtime_replay.py` executes actual NativeDispatch coroutine roots.
  A replay-only greenlet boundary parks the Python stack and yields the same
  asyncio task when the next token belongs to another producer. That producer
  consumes its own dispatch/clocks; the original stack resumes in tape order.
  There is no anonymous drain, timestamp sorting or parent-loop child-clock wait.
- Actual nested scopes, service/source/symbol/event tasks, completion callbacks,
  cancellation and terminals are validated. Disabled modules are explicit owned
  exclusions; enabled leftovers, unknown owners, parent/source mismatches and
  impossible resumes fail. Manifest identities are capture/module bound and
  independent of disabled module counts.
- `ml/native_runtime.py` binds the existing spawned InferenceWorker and reply
  relay for their full lifetimes. Startup, ready, poll availability, process
  state and shutdown are explicit observations. Availability is a recorded
  external control input; forecasts are recomputed by the real frozen child.
  Blocking IPC receive/join work is off the parent event loop. Model files are
  checked before and after replay; process/thread joins are checked.
- `ml/whole_runtime_fixture.py` replaces only REST/WebSocket IO and drives the
  real TradingEngine task architecture. Services, Bybit receive/process queues,
  symbol worker and event evaluations are enabled. The fixture does not call
  strategy/admission/broker callbacks manually and does not mutate ledgers.
  It runs real CrossVenue instrument/watch/stream/gap/reconnect/cancel roots and
  the real ShadowProbe with unchanged 10s grid, queue=32, 5ms heartbeat and TTL.
- `ml/whole_runtime_acceptance.py` freezes source/config/runtime/schema/probe/
  model before capture, validates the complete disk-backed tape and raw inventory,
  checks nonempty coverage and first-prepared plan binding, and requests two full
  F replays before projected A-E. It never launches a market session. Replay wall
  time and frozen F clocks are explicitly excluded from native cost claims.

## One unified population, with precise limits

`unified-02` contains **43,262 tokens, 5,745 tasks, 14,756 clocks and 11,271
boundaries**. One actual session contains ordinary decisions, allowed economics,
FIRE, PaperBroker entry, open position, protective managed stop close, and one
complete executable first-prepared label. All split hooks, CrossVenue lifecycle,
ShadowProbe and genuine child/relay are in this same session. Eight submitted
requests produced eight forecasts and eight unchanged probability abstentions.
There were no probe losses, writer drops or residual positions.

Every A-F variant replayed this one population once:

| Variant | Consumed | Explicit owned exclusions | Ordinary/source parity | Enabled label/child parity |
|---|---:|---:|---|---|
| A | 12,962 | 30,300 | exact | disabled |
| B | 12,970 | 30,292 | exact | disabled |
| C | 16,146 | 27,116 | exact | disabled |
| D | 18,140 | 25,122 | exact | disabled |
| E | 20,505 | 22,757 | exact | label exact |
| F | 43,262 | 0 | exact | label/forecast/adapter exact |

All enabled leftovers are zero. The F child exited with code 0; relay joined;
86 remote lifetime/request tokens were consumed. Population digest:
`ef491452c9b3ce9922e4370bda0e34c44613b9613ac064adfd5280e24eff8583`.
This development attempt used a dirty source freeze, retained verbatim. Its
legacy reports compare even disabled label/forecast hashes to F; those false
comparisons in A-D/A-E mean explicit module absence, not ordinary divergence.
The checked-in runner reports such comparisons as excluded instead.

The subsequent clean-source, indexed attempt `unified-final-01` has **43,888
tokens**. Its actual coverage checks pass, with one managed trade/label and eight
real forecasts. F repeat 1 consumes every token with exact ordinary/source/
label/forecast/adapter parity. **F repeat 2 fails with CancelledError during
coordinator stack parking.** No A-E runs follow that failure. This attempt is
NOT accepted; earlier successes do not replace its failure.

Population: `8592e27eb7fe1b35f1925c80a2a68ba9e2bc4b3fffe39678fdafe262c961d129`.
Clean capture source commit: `64e6cf5c3c406fc40687162e9dbfc022c94257c4`.

| Frozen input | SHA-256 |
|---|---|
| Source | `ec98fcac27f0e52fee7bcd22b6444219d48a6a0990029d7c958d2e7530ce4d8b` |
| Config | `e1a47dddf3c27dda0b3dd7732fb1666db0dd8d9ff38d215066f5babebb069743` |
| Runtime | `985d0d78c69938527e37a534fc04afd97cb0b440a5b47a367bd9bad0e27ae29c` |
| v5 schema | `e6fcd9fdfcd415704c16746a0ea8e0ac25adaad3349de96ddc09821ff2911e5c` |
| Probe | `fd64088bfdc7b9b8e4fe6d79044f9cd6de1620c7d0ac020096f787bd31b75dcc` |
| Model | `a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2` |
| Model manifest | `15b55fa2c360729cebb37d8cb9cbe432249907873d7016b3b81929df173082f3` |

The final diagnostic-only fix changes the source after that failed attempt;
the capture is not relabelled as an exact-final-head acceptance proof.

## Remaining blockers and next implementation boundary

1. **Owned deadlines/cancellation, R3/R5.** NativeDispatch controls observation
   order, but asyncio wait_for/wait timeouts still run on replay wall time. A
   replay pause inside a synchronous slice gives these timers an opportunity
   that did not exist in recording. A deterministic regression demonstrates a
   timeout interrupting a parked actor before its next recorded token. The
   runtime must own deadline scheduling and cancellation delivery at valid
   coroutine boundaries, including CrossVenue receive, Bybit queue waits and
   shutdown joins. Suppressing cancellation, accepting a wrong resume outcome,
   draining tokens or increasing timeouts is not a fix.
2. **Native cost estimator, R6.** Frozen causal clocks preserve decisions but
   retain F timing. Actual replay wall time includes coordinator/RPC/stack waits.
   Neither measures native B-A, C-B, D-C, E-D or F-E. An equivalent scheduler/
   source population with independent joined diagnostic spans and a recording-off
   control is still required. Whole-population instrumentation tax is NOT_MEASURED.
   Allocation throughput, GC overlap and the missing native per-variant spans
   remain null in the cost blocker report. No independent p99 values are added.

The strict F2 failure predates detailed cancellation diagnostics and cannot
identify its first cancelled token retrospectively. The deterministic deadline
regression establishes a concrete architectural defect, not a proven forensic
attribution for that particular F2 failure. Later diagnostic replays (3 and 6
passes) substitute the recorded source manifest to inspect the changed diagnostic
code. They are explicitly not source-bound acceptance, retries that erase a
failure, or additional R5 witnesses.

Actual F capture diagnostics are retained separately: 1,741 heartbeat samples,
8 joined worker items, loop p99 14.6335ms and data-to-adapter p99 46.0341ms.
These are a small synthetic all-on capture, **not eligible A-F/W2 acceptance**.
The prior qualified native reference remains 24.4621 / 308.2433ms; no measured
latency improvement relative to it is claimed.

## Defects, validation and gates

Retained receipts cover missing coordinator functionality, random manifest IDs,
optional-module shutdown await shape, transport epoch vs symbol-selection epoch,
coroutine cleanup after ownership reset, and masked cancellation diagnostics.
Epoch validation now checks each identity domain separately; selection epoch
policy is unchanged. The final cancellation change improves hard-failure evidence;
it does not resolve the deadline architecture.

Full local Windows preflight: **1734 passed**. Expanded Windows ML/spawn/codec/
CrossVenue/paired/native suites: **633 passed**. Both JavaScript syntax checks
pass. Exact published-head CI is checked after publishing this evidence commit;
its immutable run receipt belongs in the PR and delivery receipt, avoiding a
self-referential evidence-head change. The new deadline
regression passes by requiring the known unsupported case to fail explicitly;
green tests therefore cannot close the acceptance gate.

| Gate | Status |
|---|---|
| R1 exact audit/plan before code | MET |
| R2 coroutine/inter-actor execution | Implemented; deadline boundary remains |
| R3 full source/probe healthy lifecycle | Demonstrated; deadline/fault closure NOT_MET |
| R4 unified nonempty offline population | Demonstrated in one session |
| R5 strict complete-session repeatability | NOT_MET: final F2 failure retained |
| R6 production A-F cost / whole-population tax | NOT_MET / NOT_MEASURED |
| R7 profile-directed latency fix | NOT_RUN |
| R8 W2.0 controlled | NOT_MET |
| R9 one 900s attempt | NOT_RUN |
| R10 natural native-label parity | NOT_TESTED |
| Production N3.0 | NOT_MET |

[Receipts, freezes, reports and inventory](wave2-evidence/n3-whole-runtime/)
retain every attempt, including failed and diagnostic-only populations. Large
raw/tapes remain at the exact local inventory paths. No raw or historical tape
was edited to manufacture missing coverage.

Dataset `wave2-population-20260928-v1`, Maker `NO_SUPPORTED_HYPOTHESIS_YET`,
telemetry-only CrossVenue, model/strategy/risk/economics/fees/slippage/stress,
queue limits/cadences and GC policy remain unchanged. No V3 training, paired
market, applied riskScale, maker execution/ML, CrossVenue influence, Demo/mainnet,
8h/12h, 900s attempt, retry/extension or merge was performed. Test fixtures in
the standard preflight are not a V3 training or market experiment.
