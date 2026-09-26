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
