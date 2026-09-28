# W2.0 continuation: callback and queue latency

Starting head: `5e23e9840f30b6c9cf65665594d24fb886ba1ff4`, PR61 above exact PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Preserve main, PR60, trading settings and all previous raw/failed attempts.

## File audit before implementation

- `offline_study.py:advance` drains logically due arbiter/event callbacks before applying the next input. `StudyEngine` deliberately replaces the native scheduler. Do not hide runtime cost by skipping callbacks, changing order, source deadlines or probe pacing.
- `offline_benchmark.py:measure/ingest` uses bounded delivery, real-wall heartbeat and V2 shadow on each evaluation. `offline_benchmark_trace.py` separates source queue, callbacks, state, strategies, prediction and GC. Its timing still includes instrumentation and thread contention; cumulative profiles alone do not establish a cause.
- `engine.py:_evaluate` builds forming/flow context, updates structural lifecycle, evaluates density and assigned strategies, prepares/adjudicates, annotates and records. Potential duplicate public snapshots and repeated tape traversals need measurement before edits.
- `scenario_runtime.py`, `parallel_scenarios.py`, `scenario.py` create detached public views during routing/preparation. Never replace a detached view with mutable owner state or hide recorded transitions.
- `strategy/common.py:compute_trade_flow`, `strategy/pre_state.py:build_forming_candle_context`, and session book-flow snapshots scan bounded buffers. Preserve cutoffs, out-of-order/future filtering, input order and floating-point sums.
- `capture.py`, `capture_codec.py`, `recorder.py`, `ml/worker.py`: codec isolation is already present. Keep byte/row bounds, all clocks, canonical hashes, critical writer checks and inference expiry unchanged. Inspect CPU versus wall time to distinguish engine work from GIL/IPC/writer delays.

## Implementation and verification sequence

1. Preserve a fresh baseline on the identical 25045-event full-clock 58s workload (offset60, original failed-prefix raw SHA `e74cd466289d9d7411dc7d45020f605f555c912fad419234b36e6ea0021ca607`). No overlapping CPU jobs during latency runs. Record exact source/harness/workload hashes.
2. Attribute long callbacks using existing raw stage traces and bounded diagnostic CPU/wall instrumentation. Profiling results are diagnostic, never acceptance latency. Preserve all variants, including failures.
3. For each confirmed defect, write and run a failing regression before the fix. Optimize only proven redundant work; test detachment, causal ordering, clock observations and trading outputs. No queue growth, GC disabling, admission/risk loosening, reduced capture or weaker latency budgets.
4. Compare fixed populations/order and decision/portfolio hashes before/after. Require every input/clock row and healthy drained writers. Keep logical stress and native capture results distinct. Repeat only to resolve a specific uncertainty, not to select a favorable measurement.
5. Run targeted regressions and full preflight, then freeze source/config/runtime/model before at most one new 300s public-paper unified W2 capture in this continuation. No automatic retry/extension; recording/backpressure failure invalidates and stops activity. Zero natural fills remains INCONCLUSIVE.
6. Validate both chains, clocks and replay, inspect any natural prepared→economic→FIRE→fill→exit event. Update HANDOFF, roadmap/evidence and PR61; verify exact-head Linux/Windows CI. No maker/ML/registry promotion, paired/8h run, Demo/mainnet or merge without the existing gates.
