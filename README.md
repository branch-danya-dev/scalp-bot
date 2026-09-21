# Scalp Bot

Paper-first autonomous Bybit scalper prototype.

This branch is the **post-run strategy rework** based on the first live-market paper session.

## What the first paper run proved

The infrastructure worked end-to-end: scanner -> market data -> strategy decisions -> risk gate -> paper execution -> replay.

The first session exposed several implementation problems rather than one single "bad strategy":

- the same density setup could be traded repeatedly within seconds;
- active symbols were discarded too quickly when they fell out of the top activity ranking;
- profitable excursions of >=1R were sometimes given back into full losses;
- weak trades with almost no favorable excursion were held until the hard stop;
- the cost gate ignored net reward/risk;
- symbol workers raced for portfolio capital instead of comparing opportunities centrally;
- an open paper position could survive terminal shutdown without a final close event.

This branch addresses those findings.

## Horizontal levels are zones

Horizontal levels are represented as traded price zones / cascades, not exact mathematical lines.

The detector clusters nearby swing highs/lows and scores the zone using:

- separate touches;
- zone width;
- price reaction after touches;
- relative volume around touches;
- recency.

The horizontal bounce strategy works with zone boundaries.

## Stateful level breakout

New strategy: `level_breakout`.

Per-symbol state machine:

`SEARCH -> FOUND -> APPROACH -> PRESSURE -> BREAK -> IMPULSE`

It combines a traded horizontal zone with:

- repeated approaches;
- shallower pullbacks / local pressure;
- recent volume;
- Bybit public trade flow;
- taker buy/sell imbalance;
- short-term notional acceleration.

A breakout requires the whole zone to be crossed and aggressive flow to confirm the direction.

## Setup lifecycle

A detected trade is no longer just a boolean condition that can fire forever.

After a completed trade:

`ENTER -> MANAGE -> CONSUMED -> WAIT -> REARM`

The same setup ID cannot be traded immediately again. Each strategy also has a rearm cooldown. A consumed setup is reset only after the strategy has returned to a non-tradeable state for a configurable period.

This directly prevents the rapid density re-entry loop found in the first paper run.

## Active-symbol lifecycle

`candidate != active symbol`.

The scanner still promotes the most active liquid coins, but an active coin is now sticky:

- minimum active lifetime;
- separate maximum number of active symbols;
- keeps its own strategy state while being observed;
- does not disappear simply because it moved from rank 4 to rank 5;
- is deactivated only after it is old enough, idle long enough, has no position, and has no meaningful setup in progress.

## Central opportunity arbiter

Symbol workers no longer open positions directly.

They only observe markets and produce tradeable setups.

A central arbiter compares all ready setups using:

- strategy confidence;
- net reward/risk;
- current activity rank.

Only then is portfolio capital assigned.

## Trade lifecycle

The first run showed multiple losing trades that had already reached >=1R in favorable excursion.

New default management:

1. Open against a structural stop.
2. At 1R, realize 70% of the position.
3. Move the remaining runner stop to estimated **net breakeven**, including fee/slippage allowance.
4. Extend the runner target to 2.5R.
5. If the trade never develops and after 20 seconds has MFE <0.25R while moving >=0.45R adverse, cut it as `no_follow_through` instead of waiting for the full hard stop.
6. Strategy-level invalidation can also close a losing trade before the hard stop:
   - higher-timeframe direction lost;
   - horizontal/breakout zone structurally failed;
   - density confirmation disappeared.

All thresholds are configuration values and are intentionally subject to the next paper-run comparison.

## Risk / expectancy gate

A trade must now pass both:

- minimum expected net profit after fees/spread/slippage;
- minimum **net reward / net risk**.

Default:

`expected net >= $1`

`net reward/risk >= 1.15`

## Graceful stop

Pressing Stop or terminating the app finalizes all remaining paper positions and records a normal `trade_closed` event with reason `bot_stop` or `shutdown`.

## Scanner rate-limit mitigation

The activity scanner now paces its kline requests and lowers request concurrency instead of bursting requests across the full liquid universe.

## Replay observability

Replay/live UI records and displays:

- price zones;
- breakout state;
- 5-second trade flow;
- ENTRY / PARTIAL / EXIT;
- current runner stop and target;
- setup consumed / blocked / rearmed;
- expected net reward/risk;
- MAE/MFE in R;
- strategy/risk exit reasons.

## Current strategies

- trend structure / trend-line bounce;
- horizontal zone bounce;
- order-book density bounce;
- stateful horizontal-zone breakout.

## Still intentionally deferred

- per-strategy calibration from the second paper run;
- smarter density state machine beyond the generic setup lifecycle;
- trailing runner logic beyond breakeven + extended target;
- authenticated live trading;
- medium/long-term strategy layer;
- AI/ML.

The next paper run should be treated as an A/B-style comparison against the first session, not as proof of profitability.
