# Stage 7 — Semantic Arbiter

## Goal

The central arbiter no longer treats strategy-specific `setupQuality` as a calibrated cross-strategy probability score.

The selection pipeline is now:

1. collect tradeable playbook decisions;
2. apply individual semantic validity checks;
3. apply existing setup/expectancy/portfolio/RiskEngine checks;
4. compare only candidates that remain executable;
5. detect same-location playbook conflict/confluence;
6. select deterministically from shared dimensions;
7. persist the arbitration explanation into TradePlan and trade events.

## Hard semantic vetoes

### Structural path

A non-invalidated mature opposing structural level cannot sit between entry and the configured first partial-take distance.

For LONG:

- nearest mature resistance is checked;
- if its zone intersects the path from entry to first take, the candidate is blocked.

For SHORT the rule is mirrored with mature support.

A level is treated as mature when it is `worked`, has at least three distinct approaches, or at least three touches.

A breakout is exempt only when the opposing level is the breakout's own accepted zone. A second mature level in front of the breakout can still block it.

This directly protects the historical failure mode where `trend_structure LONG` entered into an unaccepted resistance that the breakout playbook was still monitoring.

### Opposing playbooks

After RiskEngine, if two executable playbooks on the same symbol express opposite directions at the same market location, both are blocked with:

`opposing_playbook_conflict`

The arbiter waits instead of choosing one interpretation by arbitrary score.

Candidates that already failed structural/risk/execution checks do not participate in this conflict calculation.

## Confluence

Same-direction executable playbooks at the same location are recorded as confluence.

Confluence does not merge stops or targets. Each playbook keeps its own plan, but confluence is the first shared selection dimension.

## Shared deterministic selection

Among allowed executable candidates, selection is lexicographic and uses only shared dimensions:

1. confluence count;
2. multi-horizon flow class;
3. liquidity alignment;
4. EntryFreshness class;
5. planned net reward/risk;
6. lower entry drift;
7. scanner market attention;
8. activity rank;
9. deterministic setup key.

`setupQuality` and playbook `confidence` are not used by this cross-strategy ordering.

They remain available as playbook-local diagnostics only.

### Flow ordering

`strongly_aligned > aligned > mixed > insufficient_data > short_term_reversal > opposed`

### Liquidity ordering

`supportive > neutral > unknown > opposed`

### Freshness ordering

`fresh > acceptable > unknown > late > exhausted`

These are shared semantic categories already produced by the preceding architecture stages, not strategy-specific formulas.

## Two-phase arbitration

Conflict/confluence is calculated only after individual semantic validation and RiskEngine planning.

This prevents an unexecutable opposite candidate from blocking an otherwise valid trade.

## Telemetry

New `arbiter_blocked` events contain:

- strategy/setup;
- blocker list;
- conflicting playbooks;
- confluence playbooks;
- structural path diagnostics;
- full semantic arbitration snapshot.

`trade_opened` and `entry_pending` now preserve:

- `semanticArbitration`;
- `selectionPriority`;
- playbook-local `playbookSetupQuality` for diagnostics only.

Session reports aggregate:

- arbiter blocked updates;
- blocker counts;
- conflicting strategy counts;
- selected confluence counts.

Analysis packs preserve deeper DOM around arbiter blocks.

## Intentionally unchanged

Stage 7 does not calibrate economics thresholds and does not add a hard veto for `late/exhausted` freshness or `liquidity=opposed` by themselves.

Those remain shared ranking/context evidence until enough post-run data exists.

## Next stage

Stage 8 adds first-class `strategy × side × regime` performance telemetry so LONG/SHORT and regime asymmetry can be measured directly rather than inferred from a handful of trade cards.
