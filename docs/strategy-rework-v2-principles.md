# Strategy Rework v2 — principles from the first paper run

This document preserves the reasoning behind the post-run patch so later tuning does not turn provisional thresholds into accidental "rules of the market".

## What was deliberately deferred before the first run

Before collecting the first session we intentionally did **not** implement:

- partial profit taking;
- a runner position;
- moving the runner stop to breakeven;
- early loss cutting before the hard structural stop.

The reason was simple: these mechanisms should be driven by observed trade paths, not guessed in advance.

The intended research loop was:

```text
paper run
  -> session.jsonl
  -> replay analysis
  -> identify real failure modes
  -> implement management rules
  -> next paper run
```

## What the first run showed

The first session gave enough evidence to justify adding this layer:

- multiple losing trades first reached a meaningful favorable excursion;
- several future losers reached around or above 1R before reversing;
- other losing trades showed almost no favorable excursion and were held until the hard stop;
- repeated entries into the same still-active setup created avoidable losses.

This means position management must distinguish at least two cases:

### A. Trade delivered the expected impulse

```text
entry
  -> favorable impulse
  -> lock part of the profit
  -> reduce remaining risk
  -> allow a runner only while the trade remains healthy
```

### B. Trade never developed

```text
entry
  -> little/no favorable excursion
  -> adverse movement
  -> setup loses evidence
  -> cut before the emergency hard stop when appropriate
```

## Current implementation

The current Strategy Rework v2 implements the first version of that logic:

```text
at ~1R favorable excursion:
  close 70%
  keep 30% runner
  move runner stop to estimated net breakeven
  extend runner target
```

It also adds early invalidation paths:

- no-follow-through exit;
- higher-timeframe context lost;
- horizontal/breakout zone invalidated;
- density confirmation lost;
- hard stop remains the final emergency boundary.

## Critical rule: the numbers are provisional

The following defaults are **not claimed to be optimal**:

- 1R partial trigger;
- 70/30 split;
- 2.5R runner target;
- 20 second no-follow-through window;
- 0.25R maximum MFE for a weak trade;
- 0.45R adverse threshold for early cut;
- current breakeven buffer.

They are a first implementation motivated by the first replay, not a finished strategy.

Every subsequent paper run must measure:

- MFE before profitable exits;
- MAE before profitable exits;
- MFE before losing exits;
- time-to-first-impulse;
- how often a 1R partial would have improved or harmed net PnL;
- how often the runner continues after the first impulse;
- how often breakeven stops remove trades that later resume;
- how much early invalidation saves versus how many valid trades it cuts.

Only after those distributions are observed should the thresholds be recalibrated.

## Broader project principle

The central research question is no longer:

> Can the bot place trades?

The first run showed that it can autonomously scan, observe, decide, execute and record.

The central question is now:

> How accurately have human trading concepts been translated into deterministic algorithms?

That applies to:

- horizontal zones;
- breakouts;
- density;
- trend structure;
- setup lifecycle;
- position management;
- risk and execution.

The strategy logic must therefore remain explainable and replayable. Any future AI layer, if one is ever needed, should improve interpretation of these concepts rather than replace observability with an opaque predictor.
