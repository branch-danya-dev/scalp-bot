# Stage 4 — LiquidityEvidence / density refactor

## Goal

`orderbook_density` is no longer an independent trading playbook.

The density state machine remains active as an order-book observer, but the engine converts any directional density signal into evidence-only telemetry before arbitration.

The tradeable playbooks remain:

- `trend_structure`
- `weak_level_rejection`
- `level_breakout`

`orderbook_density` becomes a liquidity evidence provider for those playbooks.

## Liquidity states

The normalized evidence model distinguishes:

- `tracking`
- `absorbing`
- `replenishing`
- `defended`
- `consumed`
- `removed`
- `lost_significance`
- `unknown`
- `none`

These states are intentionally not interchangeable.

### Directional interpretation

A defended / absorbing / replenishing bid wall supports upward movement.

A defended / absorbing / replenishing ask wall supports downward movement.

A consumed bid wall supports downward movement.

A consumed ask wall supports upward movement.

A removed wall is not treated as consumed. Cancellation/removal remains directionally neutral because the visible order may simply have been withdrawn.

A wall that lost significance is also neutral evidence.

## Density trade suppression

The underlying density strategy may still internally reach a directional LONG or SHORT result. The engine stores that as:

- `shadowAction`
- `shadowEntry`
- `shadowStop`
- `shadowTarget`
- `shadowConfidence`

and then converts the public decision to WAIT with `evidenceOnly=true`.

The arbiter separately skips `orderbook_density` as an additional safety invariant.

Therefore Stage 4 intentionally changes live paper behavior: density can no longer open a standalone position.

## Evidence propagation

Density is evaluated before the tradeable playbooks on every strategy evaluation cycle.

The resulting `LiquidityEvidence` is attached to subsequent strategy decisions as:

- `liquidityEvidence`
- `liquidityAlignment`

Alignment is side-specific:

- `supportive`
- `opposed`
- `neutral`
- `unknown`

Stage 4 does not yet veto a playbook based on liquidity alignment.

## Research telemetry

Session reports aggregate:

- liquidity state distribution;
- directional liquidity bias distribution;
- entry liquidity-alignment classes;
- entry liquidity-alignment by LONG / SHORT;
- average entry liquidity-alignment score.

`marketInteractionResearch` keeps density checkpoints for research, but density is excluded from playbook conflict/confluence counts.

This lets us answer questions such as:

- does a consumed ask improve breakout LONG outcomes?
- does a defended ask damage trend-continuation LONG outcomes?
- are removed walls predictive at all?
- does replenishment distinguish genuine rejection from spoof/cancellation?

## Next stage

Stage 5 combines HTF bias, LocalRegime, MultiHorizonFlowContext, LiquidityEvidence, EntryFreshness, structure/levels and execution state into a unified MarketContext consumed by all playbooks.
