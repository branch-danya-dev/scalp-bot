# Stage 5 — Canonical MarketContext

## Goal

All tradeable playbooks must evaluate the same market snapshot instead of independently reconstructing context from loosely related fields.

`MarketContext` is the canonical read-only snapshot for one symbol/evaluation cycle.

## MarketContext

The context contains:

- legacy HTF trend retained for compatibility;
- `HTFBias`;
- `LocalRegime`;
- `MultiHorizonFlowContext`;
- `LiquidityEvidence`;
- summarized `StructureContext`;
- `ExecutionContext`.

`StructureContext` contains the nearest support/resistance relative to current price, their distances, level/trendline counts and nearest support/resistance trendlines.

`ExecutionContext` contains book/candle freshness, synchronization state, spread, best bid/ask, top-5 bid/ask depth and recorded trade-buffer coverage.

## Evaluation order

One evaluation cycle now follows this order:

1. update HTF bias and LocalRegime;
2. build MarketStructure;
3. build MultiHorizonFlowContext;
4. build preliminary MarketContext without old liquidity evidence;
5. evaluate the density/liquidity provider;
6. normalize its result into LiquidityEvidence;
7. build and commit the final canonical MarketContext;
8. pass that exact MarketContext object to trend/rejection/breakout playbooks;
9. add EntryFreshness and side-specific alignment to the playbook decision.

This guarantees that playbooks in one cycle cannot observe different regime/flow/liquidity/execution snapshots.

## DecisionContext

`EntryFreshness` is setup-specific rather than market-wide, so it is not stored directly in the base MarketContext.

Instead each strategy decision receives a compact `decisionContext` overlay bound to the MarketContext fingerprint. It contains:

- market observation timestamp;
- MarketContext fingerprint;
- HTF bias;
- LocalRegime and local direction;
- side-specific flow alignment;
- liquidity state and side-specific liquidity alignment;
- EntryFreshness;
- execution readiness, spread and top-5 depth;
- nearest support/resistance distance.

`decisionContext` is copied into TradePlan strategy details, so the context used at planning time survives into trade-open and post-run analysis.

## Compatibility

Stage 5 does not yet rewrite strategy rules.

The old `trend`, `book`, `trades` and `structure` arguments remain available while every strategy also receives `market_context`.

Stage 6 will convert the strategies into playbooks that consume MarketContext directly and stop treating legacy HTF trend as the primary permission layer.

## Telemetry

Session reports now aggregate MarketContext frame coverage plus entry-time LocalRegime / HTF bias / execution readiness. Market Interaction Research includes the same context fields and EntryFreshness labels.

Trading permissions and opportunity scoring are unchanged in Stage 5.
