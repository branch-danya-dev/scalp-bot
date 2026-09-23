# Market interaction research — 2026-09-23

## Goal

The next research phase is not another threshold-tuning pass.

The question is:

> Which observable market-interaction states actually predict the next meaningful move, and when do the current strategies express contradictory hypotheses at the same location?

The current four strategy modules are treated as hypotheses about one market process:

- trend_structure -> trend continuation;
- weak_level_rejection -> level rejection / failed break;
- level_breakout -> acceptance / breakout;
- orderbook_density -> liquidity response evidence.

No trading rule is changed by this research layer.

## Research checkpoints

scalp_bot.market_interaction_review records a checkpoint when an enabled strategy changes into a meaningful interaction state.

Tracked states:

~~~text
trend_structure:
  pullback -> test -> reclaim -> continuation

weak_level_rejection:
  approach -> test -> reject -> reaction

orderbook_density:
  approach -> test -> defended -> reaction

level_breakout:
  approach -> pressure -> break -> impulse
~~~

An unchanged state/object/action is not counted repeatedly. Leaving the tracked state set resets checkpoint identity, so a later re-entry is counted as a new observation.

Each checkpoint stores:

- symbol, strategy, state and transition source;
- inferred hypothesis side;
- reference level / object generation;
- sampled market price at the checkpoint;
- strategy confidence and setup ID where available;
- flow, level-flow and local-flow metrics;
- level maturity/lifecycle metrics;
- density persistence/depletion/replenishment metrics;
- trend-pullback character;
- nearest-obstacle distance.

## Forward labels

The analyzer intentionally does not use repeated forming-candle high / low values.

Those extrema may have occurred before the checkpoint inside the same candle and would introduce look-ahead contamination.

Forward outcomes therefore use:

~~~text
research_frame.lastPrice
or
market_frame.lastPrice
or candle.close as fallback
~~~

Default forward horizons:

~~~text
30s
60s
120s
300s
~~~

Default movement bands:

~~~text
0.10%
0.20%
0.30%
0.50%
1.00%
~~~

For each horizon/band the checkpoint is classified as:

~~~text
hypothesis_first
opposite_first
ambiguous
unresolved
no_data
~~~

The thresholds are research labels, not entry gates.

## Strategy conflict / confluence

The analyzer keeps the latest active interaction state for each strategy.

When two strategies are active around the same price area, with reference levels within 0.60%, it records an overlap.

Same directional hypothesis:

~~~text
confluence
~~~

Opposite directional hypotheses:

~~~text
conflict
~~~

Conflict examples include:

~~~text
breakout LONG vs rejection SHORT at the same resistance
trend LONG vs ask-density rejection SHORT near the same location
~~~

Forward movement for overlaps is labeled neutrally as up_first / down_first. For conflicts the report also records which strategy's side won the movement race.

This is diagnostic only. No arbiter veto is added yet.

## Analysis-pack changes

The compact analysis pack now preserves deeper DOM samples around advanced interaction states:

~~~text
trend_structure:
  test / reclaim / continuation

weak_level_rejection:
  test / reject / reaction

orderbook_density:
  test / defended / reaction

level_breakout:
  break / impulse
~~~

This is required to study:

- wall depletion;
- replenishment;
- absorption;
- local queue/flow changes;
- whether a displayed wall remains meaningful after the original confirmation.

The ordinary periodic order-book sample remains in place outside those windows.

## Initial sanity check: session-20260923T083442Z

The existing compact session was used only to verify that the research labels distinguish materially different states.

Approximate analyzer output:

~~~text
interaction checkpoints: 347
same-location overlaps:   207
conflicts:                 84
confluences:              123
~~~

At the 120-second / 0.20% label:

| Strategy / state | Total | hypothesis first | opposite first | unresolved | resolved directional rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| level_breakout / break | 3 | 3 | 0 | 0 | 100% |
| trend_structure / continuation | 5 | 2 | 2 | 1 | 50% |
| orderbook_density / test | 18 | 10 | 2 | 6 | 83.3% |
| orderbook_density / reaction | 16 | 7 | 3 | 6 | 70% |
| weak_level_rejection / reaction | 2 | 0 | 2 | 0 | 0% |

These numbers are **not sufficient to promote, disable or tune a strategy**:

- terminal-state samples are tiny;
- multiple checkpoints can belong to one underlying interaction episode;
- one session represents one market regime;
- directional movement does not yet include execution economics.

The useful result is that the measurement layer can separate states that looked identical in the old PnL-only report.

## Research sequence

1. Run untouched paper sessions with all current strategies enabled.
2. Build the normal analysis pack.
3. Read marketInteractionResearch from session-report.json, or run:

~~~powershell
python scripts/build-market-interaction-report.py path\to\session.jsonl
~~~

4. Aggregate state-level forward labels across sessions.
5. Split results by:
   - trend regime;
   - level kind/maturity;
   - conflict vs confluence;
   - flow participation;
   - absorption/depletion;
   - distance to obstacle;
   - execution mode.
6. Only then decide the architecture changes:
   - which states become actual playbooks;
   - whether density remains a standalone strategy or becomes evidence only;
   - which same-location conflicts require veto/arbitration;
   - which confirmations must be revalidated immediately before entry.

## Decision standard

A single successful run is not enough.

Before a trading rule is promoted from research to production logic, inspect at minimum:

- sample count and number of distinct interaction episodes;
- directional first-hit rate at multiple horizons/bands;
- MFE/MAE timing;
- results across more than one market regime;
- conflict/confluence behavior;
- execution-adjusted expectancy.

The research layer is deliberately descriptive. It should tell us what the market did after each hypothesis without changing what the bot trades during the observation period.
