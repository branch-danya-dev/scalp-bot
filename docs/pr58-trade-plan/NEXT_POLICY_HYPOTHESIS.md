# One hypothesis for separate approval: align the decision with a causal ready event

Status: PROPOSED_ONLY. Not implemented, not trained, not enabled. launch_authorized=false. No change to PR59 goals, duration, selected mode or schedules.

Hypothesis: evaluate the same30bps target /15bps stop /30s /100ms /100USDT plan once at the first causal ready event of an existing engine scenario, rather than treating arbitrary10-second both-side observations as equivalent entry opportunities. This changes only the decision observation population/time; it does not widen a stop, extend a horizon, lower0.55 or introduce a new ordinary-bot filter. Exact scenario identity, one observation per episode and causal availability must be frozen before outcome computation. Ordinary routing/risk stays identical.

Limited rationale: current validation mostly has too little movement to pay costs (median forming range about5bps; mean timeout net -0.120343). Existing event-to-grid alignment has23/26 ready events with an older-than1s or missing preceding sample. These facts justify testing a timing hypothesis; they do not demonstrate that existing ready events are profitable. Indeed the ordinary ledger includes losing ready events, and higher V2 p_target still has worse mean net.

Approval would cover a separately registered offline experiment only. Retain the present grid/V2/0.55 as an immutable control; freeze event predicate, one-per-episode matching and temporal boundaries before labels. Include all qualifying events, costs, missing-data exclusions and positive/negative controls. Separate train/calibration/validation and a genuinely untouched later period; published V2 test cannot be reused for selection. No V3 or new labels for this hypothesis were produced in this task.

Reject if an adequate set of dependent time groups fails to show positive net utility across documented periods, if benefit is concentrated in one instrument/event, or if causal sampling/latency assumptions do not hold. Any eventual trading claim also requires an independent one-owner/shared-risk portfolio comparison including displaced ordinary trades and actual full-path latency. Positive overlapping-label averages alone are insufficient. Do not relax sample adequacy, costs, risk or latency budgets to obtain acceptance.

The owner must separately approve this experiment's registered event definition and fresh holdout before implementation. It provides no authorization for a market run or the eight-hour test.
