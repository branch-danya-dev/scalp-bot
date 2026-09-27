# Trading model implementation plan — 2026-09-28

Base: `dfcc5949f2b5cb6f902e304dfbe5bd1f4a7b3132`, verified against GitHub main.
Branch: `codex/trading-model-admission`. Isolated local clone; original captures and credentials remain outside it.

## Audit findings

1. `strategy/playbook_context.py:assess_entry_context` returns early for **any** assigned scenario, skipping flow, hourly override constraints and execution readiness. `_assigned_plan` also substitutes ownership for local policy. `scenario_runtime.py:_scenario_entry_valid` does not refresh context admission at final dispatch.
2. Both level strategies skip context-loss on scenario-owned positions. `position_context_supported` only checks allowed direction (both sides for most level regimes), so merely removing the early return is insufficient. Engine grace also excludes scenario positions. Retain structural invalidation/protective stops; debounce new context loss using fresh observations and keep owner identity fixed.
3. `StrategyDecision.tradeable` means geometry present, not economic executability. `ScenarioRouter.accept_decision` freezes this causal hypothesis and sets ARMED before risk; freshness must continue from original confirmation even after rejection. Existing `_scenario_prepare` is only an unreserved preview and must not become an execution authorization.
4. `_arbitrate_once` evaluates all candidates using the same budget, then submits one global maximum. Sequential dispatch must rebuild each plan against the post-reservation budget and preserve one symbol owner, staged adds and paired Demo reservation.
5. Strategy expectancy gate **already exists** in arbiter. It is opt-in and sample-limited, with strategy-wide realized statistics. Extend it to segment evidence and expose sample sufficiency/shadow decisions; conditional target payout remains distinct.
6. `InputJournal.append` validates, JSON-roundtrips and hashes synchronously. Recorder already has one bounded FIFO writer and explicit dropped-row accounting. Offload hashing/encoding into that same ordered writer; detach mutable bodies before enqueueing with a cheaper safe copy. Missing rows/suffixes must still fail validation.
7. Pending entry tick path calls full `_evaluate` after each tick. `_validate_pending_entry` already operates on the immutable owner plan: add a narrow current-context refresh and reuse that validator after fill, before next tick.
8. Existing `ml/engine_dataset.py` samples a time grid and both directions. V3 must have a separate first-prepared collector/schema and fixed validation policy; V2 defaults, threshold and artifacts remain untouched.

## Implementation sequence and tests

### A — Context policy and management (P0.1–2)
Files: `strategy/playbook_context.py`, `strategy/{breakout,weak_level_rejection}.py`, `scenario_runtime.py`, `engine.py`.
Regressions: new `tests/test_scenario_context_policy.py`; existing `test_playbook_context`, `test_breakout_hourly_context`, `test_scenario_execution`, `test_scenario_remediation`, engine lifecycle. Cover both sides, wrong owner/side, opposing flow/HTF, legitimate 1h-only override, fresh final admission, transient versus sustained loss, immutable owner and immediate hard stops.

### B — Admission boundary and sequential budget (P0.3–4, P2.18)
Files: new `admission.py` (PreparedIntent/EconomicPlan and admission stages), `domain.py`, `scenario_runtime.py`, `engine.py`, scenario lifecycle telemetry.
Regressions: new `tests/test_admission_pipeline.py`; scenario/parallel/semantic arbiter, freshness, risk and Demo tests. Reject geometry/economics before FIRE; preserve confirmation timestamp; multiple symbols per pass; replan second candidate after reservation; no duplicate symbol, over-budget fills or orphan adds.

### C — Capture and pending hot path (P1.9–11, P2.18)
Files: `input_journal.py`, `recorder.py`, new `market_runtime.py`, `engine.py`, latency instrumentation.
Regressions: new journal writer and pending causal tests plus existing journal v1–v4, recorder, capture replay, lifecycle/transport suites. Mutable input isolation, exact hash chain, dropped middle/suffix detection, flush/error behavior, no full strategy graph per pending tick, fill-before-invalidation ordering.
Profile recorded capture through real writer with stage distributions and IDs. Keep queue limits unchanged; distinguish public network failures from measured CPU stages.

### D — Edge evidence (P1.5–8)
Files: `expectancy.py`, `admission.py`, `config.py`, new `setup_segments.py` and offline CLI; tests for segmentation and evidence gates.
Dimensions: strategy, trend relation, local regime, HTF, flow, target source, stop distance, cost share. Closed-position ledger contributes portfolio PnL once; overlapping ready labels form a separate diagnostic population. Report missing/censored evidence and per-strategy limitations. Default observe-only; enforce negative segments only with sufficient valid prior observations and explicit configuration.
Read archived PR58/recent captures without modifying them. No claim of validated edge from the previously studied periods.

### E — Causal ML V3 (P2.12–17)
Files: separate `ml/prepared_dataset.py`, validation/split helpers and checked-in V3 protocol, tests under `tests/ml`.
Freeze features once per causal first-ready identity, including economically rejected intents. Side/geometry come from the prepared strategy. Label only later executable paths with fixed costs and censoring; calibrate plan candidates on training only, freeze before validation. Purge overlapping label intervals, embargo, walk-forward and leave-symbol-out; ranker/veto outputs route through common admission. No V2 threshold lowering or autonomous ML orders.

### F — Verification, smoke, handoff
Run `scripts/test_preflight.py`, Windows optional-ML/spawn CI suite and JS syntax checks; run Linux equivalent if available, otherwise report exact platform limit. Short paper-only capture smoke after P0/P1 tests; no mainnet/Demo order calls, no 8h/12h run. Zero fills is inconclusive, never a pass. Record stage p99, backpressure, writer drops, economic rejection stage and natural fills. Update 8h A/B protocol with source/config/model hashes, launch gates and HANDOFF with commits/results/unresolved issues.

For each confirmed bug, save a failing regression run before its patch, then the passing run. Do not loosen costs, slippage, stress or caps to obtain fills.
