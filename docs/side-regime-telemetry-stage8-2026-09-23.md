# Stage 8 — Strategy × Side × Regime telemetry

## Goal

Measure directional asymmetry explicitly instead of inferring it from a handful of trade cards.

The primary research key is:

`strategy × side × LocalRegime`

Examples:

- trend_structure × LONG × bullish_trend
- trend_structure × SHORT × bearish_trend
- level_breakout × LONG × range
- weak_level_rejection × SHORT × bearish_trend

## Actual trade performance

For every closed trade the report reconstructs the planning-time context from the matching `trade_opened` event and groups the result by strategy, side and LocalRegime.

Per group the report includes:

- trades / wins / losses / breakeven / win rate;
- gross PnL / fees / net PnL / average net PnL;
- average and median realized R;
- average and median MFE R / MAE R;
- average and median planned net reward/risk;
- average and median EntryFreshness move-spent ratio;
- average and median confirmation age;
- average holding duration;
- partial-take rate;
- freshness class distribution;
- multi-horizon flow-alignment distribution;
- liquidity-alignment distribution;
- HTF-bias distribution;
- exit-reason distribution;
- confluence-count distribution.

Realized R is calculated as net PnL divided by the initial risk USD recorded by the paper broker.

Legacy trades without a planning-time MarketContext remain in the dataset under `regime=unknown`; they are not silently discarded.

## Independent hindsight coverage

Hindsight opportunities remain independent from actual trade PnL.

For every hindsight opportunity, each strategy is grouped by:

`strategy × opportunity side × oracle-entry LocalRegime`

and classified as:

- unaware;
- observed_aligned;
- tradeable_aligned;
- opposed.

The coverage matrix records:

- number of opportunities;
- opportunities observed by that strategy;
- opportunities reaching a tradeable aligned state;
- opportunities actually traded by that strategy;
- observed coverage rate;
- tradeable coverage rate;
- trade coverage rate;
- bot-comparison classifications.

This lets us distinguish:

1. the market offered fewer LONG opportunities;
2. the playbook failed to detect LONG opportunities;
3. it detected them but did not become tradeable;
4. it became tradeable but arbiter/risk/execution did not select them;
5. it traded them but expectancy was poor.

## Live telemetry

Each strategy card keeps a lightweight live LONG/SHORT summary by LocalRegime:

- trades;
- wins/losses;
- win rate;
- gross/fees/net;
- average MFE R;
- average MAE R.

This live view is convenience telemetry. The post-run performance matrix is the authoritative detailed analysis because it preserves planning-time context and hindsight labels.

## Session report

The canonical report field is:

`strategySideRegimePerformance`

It contains:

- `sideSummary`;
- `byStrategySide`;
- `bySideRegime`;
- `byStrategySideRegime`;
- `hindsightByStrategySideRegime`;
- paired closed-trade records used by the aggregation.

The same matrix is exposed by the Opportunity Review API for the selected session.

## UI

Opportunity Review now shows a Strategy × Side × LocalRegime section before the hindsight opportunity list.

It displays actual PnL/MFE/MAE/planned-RR/freshness plus the independent hindsight coverage for the same key when available.

## Next stage

Stage 9 calibrates economic policy using these conditional groups. Universal thresholds such as `net R:R >= 1.15` should only become hard policy where strategy × side × regime evidence supports them.
