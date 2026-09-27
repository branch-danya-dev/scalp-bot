# Parallel scenarios contract v3

Shared candles, books, flow, structure and applicability are computed once. Each
(symbol, strategy) has one independently selected object and lifecycle. Identity
includes owner, direction, objectRef/generation and causal episodeKey. Density
remains evidence only; configured enable flags are unchanged.

Observation -> independent ASSIGNED/PREPARED -> frozen ARMED proposals -> one
synchronous dispatcher -> RESERVING -> ORDER_PENDING -> IN_POSITION. Cancellation
and reconciliation retain the execution owner until acknowledgement; late fills
restore the original contract. Protective exits and same-owner staged adds retain
existing broker semantics and share portfolio limits.

Within a symbol choose earliest firstSignal among complete, fresh, executable and
risk-admissible proposals; ties use level_breakout, weak_level_rejection,
trend_structure, price_action_hypothesis, then scenario ID. This rule was fixed
before comparison. No voting delay, confidence sum or maximum planned R:R.
Cross-symbol selection retains existing scanner priority and shared budget.

Each strategy receives a replaced immutable MarketContext with only its own
scenario. The shared session context is never changed by the last evaluator.
Errors and WAIT are local. Sequential bounded evaluations use one event loop;
CPU-hung strategy code is not preemptible and remains a latency limitation.

Execution completion/cancel acknowledgement invalidates waiting confirmations;
a fresh causal episode is required. Transport epoch change clears readiness and
preparation, requires snapshot and fresh confirmations, but keeps trade protection.

scenario_transition and scenarioRouting have schemaVersion=3. scenarios,
readyProposals, execution and busyReason distinguish preparation from ownership.
The old scenario summary is a compatibility representative only. Legacy v2
captures and the per-owner ScenarioRouter lifecycle tests remain readable.


## Migration and review gates

Former tests that required only one evaluator or identity with the shared context
now assert one private scenario per applicable strategy and shared flow/execution
objects. Confirmed candle-only inputs, frozen plans, causal object episodes,
XRP reset, six execution controls and pending/late-fill safeguards remain tested.
The new priority is independent of the dictionary/evaluation iteration order.

Risk refusal is attached to a strategy plan. A source-independent fingerprint of
symbol/side/object/episode/entry/stop preserves original target and first-ready age
when the same causal plan is renamed. Incomplete/nonfinite geometry is local WAIT;
geometry cannot be repaired by another strategy label. Independent distinct plans
can pass. Selection makes no waiting/voting window; current price and shared risk
are rechecked synchronously before execution ownership is installed.

One candidate per strategy is chosen using the existing ordered applicability
assessment restricted to that strategy. No independent scanner/feed was added.
Alternatives such as confidence averaging or max R:R were rejected before testing:
the scales are not comparable and target extension must not win admission.
Cross-symbol exposure, pending budget and same-owner protective operations retain
the existing executor. Busy state also exists when no broker position is visible.

Default profile remains breakout/rejection on, trend/BETA off and density evidence
only. ML proposals remain a separate offline shadow diagnostic with no reservation.
Manifest v5 records the optional rejection-policy switch; manifest v4 field sets
are frozen. Input transport resets are explicitly scoped in newly recorded journals.
The stopped baseline uses its saved source/dependencies; the patch is not claimed
to reproduce that incomplete source capture as a certified full replay.

Latency gate: 62 packets at archived cadence and scenario bookkeeping are measured
in ../implementation/transport-load.json. A synchronous CPU-hung strategy remains
unpreemptible; no separate strategy processes were introduced. Portfolio PnL of
parallel preparation has not been computed by deleting or replacing old trades.

## Offline verification after af988946

`offline_study` uses actual engine/risk/execution classes with a deterministic per-variant scheduler. A wraps the original ScenarioRouter as the single preparation primitive; B/C keep independent preparation. This harness never starts exchange workers. Its fixed captured membership and source availability are explicit counterfactual conditions, not original-scheduler certification. See [completed readiness study](../../PR58_READINESS_REVIEW.md). Production ownership/risk/defaults did not change.
