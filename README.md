# Scalp Bot

Paper-first prototype of an autonomous Bybit scalper.

This branch contains the first strategy-model rework prepared **separately from the running paper session**. It is intended to be combined with findings from the current replay analysis before the next paper run.

## Strategy model rework

### Horizontal levels are price zones

A horizontal level is no longer represented as one exact price. The detector clusters nearby swing highs/lows into a `LevelZone` and evaluates:

- number of separate touches;
- width of the cascade / traded range;
- how strongly price reacted after touches;
- relative volume at those touches;
- how recently the zone was active.

The horizontal bounce strategy trades the zone boundaries, not an arbitrary mathematical line.

### Stateful level breakout

New strategy: `level_breakout`.

It follows a per-symbol state machine:

`SEARCH -> FOUND -> APPROACH -> PRESSURE -> BREAK -> IMPULSE`

The state is isolated inside each active symbol session. A breakout on one coin cannot affect another coin.

For an upward breakout the bot:

1. Finds a traded resistance zone with repeated reactions.
2. Watches price approach instead of rediscovering the level on every tick.
3. Looks for pressure near the zone:
   - repeated closes near the level;
   - shallower pullbacks / rising local lows;
   - increased recent volume;
   - taker-side public trade flow;
   - acceleration of recent traded notional.
4. Requires price to cross the **zone**, not just one exact line.
5. Requires post-break trade flow to remain aligned with the breakout direction.
6. Places invalidation beyond the opposite side of the zone plus a local range buffer.
7. Rejects the setup when the structural stop is too far for a scalp.
8. Targets the first plausible impulse. The existing cost gate still decides whether the move is large enough after fees/spread/slippage.
9. Marks the zone as already used so it does not chase the same breakout repeatedly.

Downward support breaks are mirrored.

### Market activity / volume context

Active symbol sessions now keep a rolling Bybit public-trade tape. Replay frames include a 5-second trade-flow summary:

- taker buy notional;
- taker sell notional;
- buy/sell imbalance;
- traded notional per second;
- acceleration versus the preceding 15 seconds;
- trade count.

This is deliberately not interpreted as "green candle = buyers" or "large volume = money entered long". It measures executed aggressive flow and is used only as confirmation around an already identified structure.

## Current strategies

- trend structure / trend-line bounce;
- horizontal **zone** bounce;
- order-book density bounce;
- traded horizontal-zone breakout.

## Existing safety / execution rules

- Bybit public market data; paper execution only.
- Liquid-universe filter and current-activity ranking.
- Higher-timeframe direction filter.
- One open position per symbol; multiple symbols may be traded asynchronously.
- Shared portfolio exposure and risk limits.
- Bid/ask-aware paper fills plus modeled slippage.
- No-chasing entry-drift gate.
- Expected profit must cover fees + spread + slippage + minimum net profit.
- MAE/MFE recording and visual session replay.

## Deliberately deferred until paper-run analysis

- partial profit taking + runner position;
- moving the remaining stop to breakeven after the first impulse;
- dynamic early invalidation before the hard structural stop;
- recalibration of all thresholds from replay evidence;
- broader medium/long-term strategy layer;
- AI/ML.

These should be driven by the current session data rather than guessed before the replay review.
