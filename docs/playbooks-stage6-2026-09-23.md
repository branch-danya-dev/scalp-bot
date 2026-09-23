# Stage 6 — MarketContext-driven playbooks

## Goal

The three tradeable strategy modules now behave as playbooks selected from `MarketContext` rather than treating legacy 15m/1h `UP/DOWN/FLAT` as the primary permission layer.

Tradeable playbooks:

- `trend_structure` -> `trend_continuation`
- `level_breakout` -> `level_breakout`
- `weak_level_rejection` -> `level_rejection`

`orderbook_density` remains an evidence-only liquidity provider.

## Direction policy

### Trend continuation

Continuation direction comes from LocalRegime:

- bullish trend / bullish impulse -> LONG context;
- bearish trend / bearish impulse -> SHORT context;
- pullback -> inherit the 5m parent direction;
- transition / range / unclear -> no continuation playbook.

HTF bias remains context. An opposing HTF bias is recorded but does not veto a locally coherent continuation.

At the final entry stage, continuation additionally rejects:

- `flowAlignment=opposed`;
- `flowAlignment=short_term_reversal`.

The latter directly targets the observed failure mode where the last 5 seconds reverse bullish while 15s/60s flow remains bearish, or vice versa.

### Breakout

Breakout direction comes from the local market state:

- bullish trend/impulse -> resistance breakout;
- bearish trend/impulse -> support breakdown;
- pullback -> parent-trend breakout;
- range -> both boundaries are eligible;
- transition -> current transition direction;
- unclear -> wait.

This means a genuine range breakout is no longer impossible simply because legacy HTF context is `FLAT`.

At final entry, a breakout is blocked only when the multi-horizon flow is explicitly `opposed`. `short_term_reversal` is not blocked for breakout because a real breakout may itself be the event changing the longer flow regime.

### Level rejection

Rejection direction policy:

- bullish trend/impulse -> support rejection LONG;
- bearish trend/impulse -> resistance rejection SHORT;
- pullback -> rejection in the parent direction;
- range -> support LONG and resistance SHORT are both valid playbooks;
- transition / unclear -> wait.

The level selector filters out zones that cannot produce an allowed rejection direction, instead of selecting a nearer counter-context zone and blocking only at the end.

## Position management

Context-loss exits for trend, breakout and rejection now use the current MarketContext policy. A position opened from a valid local LONG is no longer closed merely because legacy HTF remains `FLAT`.

Strategy-specific premise invalidation remains primary:

- breakout accepted back inside the broken zone;
- rejection invalidated through its level;
- structural stop and paper-broker management.

## Compatibility

If a strategy is called without MarketContext, the old legacy trend is used as a fallback. This keeps isolated regression tests and external callers deterministic while the live engine uses the new architecture.

## Research telemetry

Each decision now exposes:

- `playbookContext`;
- allowed directions;
- primary playbook direction;
- context source;
- `entryContextAssessment` and blockers.

Session reports aggregate playbook context sources, selected directions and context blockers. Market Interaction Research stores the same fields for forward-label analysis.

## Next stage

Stage 7 rewrites the central arbiter around semantic conflict/confluence between playbooks and MarketContext. It will stop ranking unrelated strategy-specific `setupQuality` values as if they were calibrated on one common scale.
