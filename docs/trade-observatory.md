# Trade Observatory

Trade Observatory is the analysis and debugging layer for paper/live trading. It does **not** change strategy eligibility, position sizing, or execution rules.

## Decision Trace

Every strategy decision is normalized into a trace containing:

- observation timestamp;
- strategy and internal state;
- current trend context;
- watched market object: horizontal zone, trendline, density wall, or price level;
- confirmed conditions;
- conditions still being awaited;
- entry / stop / target when present;
- selected flow, density, liquidity and quality evidence.

Saved sessions created before Decision Trace existed are normalized on read when the old payload contains enough information.

## Trade Review

A closed position is reconstructed from its opening/closing events and surrounding recorded market data.

Each review contains:

- symbol, strategy, side and setup ID;
- entry, stop, target and exit;
- notional, fees, gross/net PnL;
- MAE/MFE in USD and R;
- exit reason and partial status;
- pre-entry and in-trade decision timeline;
- chart candles;
- market/research frames;
- open/close market snapshots.

The UI renders these as expandable **Trade Review Cards** with an interactive chart and timeline.

## Market Inspector

The live chart supports:

- 5s;
- 15s;
- 1m;
- 5m;
- 10m;
- 15m;
- 1h.

Layer controls:

- **Active** — only objects currently watched by strategies;
- **HTF** — higher-timeframe structural levels;
- **All** — broader structural context;
- separate trendline toggle.

Entry, stop and target remain visually dominant when a position exists.

## DOM Inspector

When the density strategy is tracking a wall, the order-book panel exposes:

- wall side and price;
- displayed notional;
- strength versus local baseline;
- remaining ratio;
- attacking notional over 5s;
- depletion rate;
- replenishment ratio;
- local executed-flow imbalance;
- best-level OFI;
- absorption / wall-present / invalidation flags.

The watched wall is highlighted directly in the displayed book when it is inside the selected DOM depth.

## Decision Inspector

SEARCH / FOUND / APPROACH / TEST / REJECT / BREAK / etc. are shown together with:

- strategy;
- exact watched object;
- observation time;
- trend context;
- confirmed evidence;
- explicit "waiting for" conditions.

The decision log can be filtered by:

- strategy;
- decision;
- entry;
- reject;
- exit;
- error.

## Strategy analytics

Each strategy maintains runtime counters for:

- tradeable signals;
- closed trades;
- wins/losses;
- risk rejects;
- net PnL.

These counters are for current runtime observability. Historical session analysis comes from the recorder.

## Offline Opportunity Review

This module runs on **recorded** data only.

Rejected tradeable decisions are replayed over a future horizon using their original:

- side;
- entry;
- stop;
- target.

The analyzer reports:

- target-first;
- stop-first;
- ambiguous same-frame;
- unresolved;
- MFE in R;
- MAE in R.

For actual non-target exits it also checks:

- whether the original target was reached after exit;
- post-exit MFE in R.

These classifications are **review candidates**, not automatic proof that a strategy rule is wrong. Their purpose is to identify where chart/timeline inspection should begin.

## Reviewing a saved session

1. Start the updated application.
2. In **Trade Observatory**, select any saved `session-*.jsonl`.
3. Closed Trade Review cards load from the selected session.
4. Open a card to inspect chart + decision timeline.
5. Press **Пересчитать** in Post-run Opportunity Review.
6. Compare missed-target-first and early-exit candidates with the original decision trace.
7. Only then decide whether a strategy rule needs adjustment.

The one-hour research session recorded before this feature branch is supported through legacy normalization where the old payload contains sufficient context.
