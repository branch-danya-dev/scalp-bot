# Trading methodology audit — 2026-09-22

This note records the external methodology review performed before the next paper run. It is intentionally about trading logic and market microstructure, not about optimizing a historical PnL result.

## Reference principles

### Context before trigger

Professional order-flow material consistently treats tape/DOM as confirmation at an already meaningful location, not as a standalone directional signal.

- SMB Training repeatedly combines important technical levels with tape confirmation and describes support/resistance as inflection points whose validity must be confirmed by actual buying/selling behavior.
- Jigsaw distinguishes executed order flow from passive liquidity and stresses that trade size is relative to the instrument.
- Bookmap material frames the decision as: market structure / liquidity area first, then observe what happens when price tests it.

This supports the bot's current sequence:

```text
HTF context -> structural level -> approach/test -> executed-flow/liquidity response -> entry
```

### Trend structure

A trend is a sequence of higher highs + higher lows or lower highs + lower lows. A wick through a prior extreme is not sufficient continuation evidence if price is not accepted beyond it.

Current policy after the audit:
- only confirmed 1m/5m/15m/1h candles feed structure;
- 15m direction is vetoed by an opposing 1h structure;
- the structural continuation classification requires a close beyond the prior swing extreme;
- trend pullbacks reject an aggressive countertrend pullback character;
- the entry still requires test -> reclaim -> current relative tape participation -> follow-through.

### Rejection / failed breakout

A rejection is not "price came close to the edge". It requires an actual attempt beyond the area followed by failure to hold there.

Current policy after the audit:
- candle must trade beyond the zone and close back through it;
- executed trades must exist strictly beyond the broken edge;
- reversal confirmation must come from fresh tape at the level, not unrelated prints elsewhere;
- countertrend rejection entries are blocked;
- after entry, a return through the invalidation area must persist before the premise is considered failed.

### Breakout

A breakout needs acceptance, not only a wick beyond resistance/support.

Current policy:
- mature/worked shared level;
- directional pressure into the level;
- executed flow strictly beyond the edge;
- relative tape participation;
- executable price acceptance outside the zone for a minimum hold;
- if price is accepted back inside after entry, the breakout premise is invalidated.

### Visible liquidity / density

Displayed size is not trusted just because it is large. Real/fake liquidity material emphasizes persistence and what happens as price approaches the order.

Current density policy:
- absolute USD floor;
- relative strength versus neighboring depth;
- activity-scaled floor;
- time persistence;
- actual approach/touch;
- depletion / replenishment / aggressive attack measurement;
- wall removed before defense -> reject;
- wall tested and defended -> require local tape reversal;
- adverse price acceptance + flow through the old wall -> invalidation.

### Tape participation

A few prints do not prove initiative. Size and speed are market-relative.

Current policy:
- 5s current window compared with the preceding 15s;
- a causal baseline is mandatory;
- entry confirmation requires current executed notional/second to be at least the baseline pace;
- trade-rate and average-size ratios remain diagnostics;
- directional imbalance/CVD/local-flow rules remain strategy-specific.

### OFI

Best-level order-flow imbalance is recorded because empirical microstructure research finds short-horizon price changes strongly related to order-flow imbalance, with impact dependent on depth.

It is telemetry only for now. It is **not** a hard entry gate yet because no Bybit-specific calibration has established a robust threshold for this bot.

### Execution quality

For a scalp, arrival price and fill quality are part of the strategy.

Current policy after the audit:
- ticker spread is observed during universe selection;
- a symbol is rejected early if half-spread already makes the best quote incompatible with the configured entry-drift rule;
- full live book freshness/synchronization remains mandatory;
- RiskEngine calculates entry economics using visible-depth VWAP;
- PaperBroker walks visible depth rather than filling the whole order at the best quote;
- configured slippage is an additional reserve, not a replacement for depth impact.

### Risk

The stop is structural: it belongs where the trade thesis is wrong. Position size adapts to that distance.

Current policy:
- per-trade `risk_fraction` means planned **all-in** loss at stop, including fee/slippage reserve;
- structural price loss is recorded separately;
- aggregate open all-in risk remains capped;
- economic gate checks minimum net profit and net reward/all-in-loss geometry.

### Trade management

Premise invalidation takes precedence over the current PnL sign.

Current policy:
- breakout accepted back inside -> exit after the required persistence;
- weak-level invalidation and density price/flow invalidation can close a positive trade too;
- partial requires both MFE threshold and positive closed-leg economics;
- runner stop protects true net breakeven;
- a detected structural liquidity target is not mechanically moved farther away after partial.

## Changes deliberately not made

These remain empirical parameters, not methodology bugs:

- exact entry imbalance thresholds;
- 1R partial trigger;
- 70/30 partial split;
- 20s no-follow-through timer;
- 0.25R MFE / 0.45R adverse early-cut values;
- fallback runner 2.5R;
- hard OFI threshold.

Changing them before a new, untouched observation would be parameter tuning without evidence.

## External references

- CME Group — Proper Position Size:
  https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size
- CME Group — CME Liquidity Tool User Guide:
  https://www.cmegroup.com/education/demos-and-tutorials/cme-liquidity-tool-user-guide
- CME Group — Assessing Liquidity:
  https://www.cmegroup.com/education/articles-and-reports/assessing-liquidity
- Bybit V5 API — Get Tickers:
  https://bybit-exchange.github.io/docs/v5/market/tickers
- Bybit V5 API — Get Orderbook:
  https://bybit-exchange.github.io/docs/v5/market/orderbook
- Jigsaw Trading — Reconstructed Tape / relative trade size:
  https://www.jigsawtrading.com/learn-to-trade-free-order-flow-analysis-lessons-lesson6/
- Jigsaw Trading — Order-flow analysis lessons:
  https://www.jigsawtrading.com/free-order-flow-analysis-lessons/
- SMB Training — order flow at important levels:
  https://www.smbtraining.com/blog/traders-ask-how-do-i-read-the-order-flow-in-an-etf
- SMB Training — support break and quick re-bid:
  https://www.smbtraining.com/blog/how-to-use-tape-reading-to-enter-a-profitable-swing-trade
- SMB Training — futures order-flow shift at support:
  https://www.smbtraining.com/blog/smb-futures-totd-5-23-18
- Bookmap — Order Flow and Market Structure:
  https://bookmap.com/learning-center/en/market-mechanics/bookmap-education-course/order-flow-and-structure
- Bookmap — ES Retesting Strength:
  https://bookmap.com/learning-center/order-flow-phenomena/order-flow-education/es-retesting-strength
- Bookmap — Real vs Fake Liquidity:
  https://bookmap.com/learning-center/order-flow-phenomena/order-flow-education/real-vs-fake-liquidity
- Bookmap — price breakdown and rejection:
  https://bookmap.com/learning-center/order-flow-phenomena/order-flow-education/bookmap-webinar-es-price-breakdown-and-rejection
- Cont, Kukanov, Stoikov — The Price Impact of Order Book Events:
  https://arxiv.org/abs/1011.6402
