# W2 research contract and gates

This protocol is frozen before using outcomes to select features or models.
Ordinary strategies, fees, slippage, stop-depth stress and risk caps retain PR60
defaults. The W2 observer is explicitly injected only by `--wave2`; normal app
startup does not import learning code or start external connections.

## Observation definitions

The Segment Registry uses the existing eight dimensions and SegmentExpectancyBook.
Closed positions and independent prepared shadow labels occupy distinct populations
inside that store. No aggregation of overlapping label PnL is a portfolio result.
Per-symbol overlapping shadow intervals are excluded from lifecycle fitting;
censored observations do not count. Recent history: 200 accepted observations;
minimum long/recent samples: 100/30; at least three capture IDs; residence: 1h.
Promotion requires both descriptive 95% lower bounds >0.05R; demotion uses upper
bounds <-0.05R or diagnostic drawdown 5R/10R. Bounds are normal approximations,
not a proof of independence. Risk scales 0/.25/1/.5/0 are recorded as **would**
values only. They never change portfolio decisions or redistribute unused risk.

Cross-venue windows are 500ms and 1000ms, based on local processing availability.
Binance `depth5@100ms` and OKX `books5` have approximately 100ms cadence; 100ms
return claims would be too sensitive to arrival timing. A baseline quote must
precede the window boundary and be no more than 250ms older. Quotes expire after
1500ms; gaps/reconnects reset windows. Exchange timestamp, receipt UTC, receipt
monotonic and processing monotonic are retained independently. Exchange clocks
are not declared aligned; receipt minus exchange outside [-400,2000]ms rejects
coverage. Binance previous-update discontinuities reset snapshot history. Only
exact base/USDT perpetual matches are subscribed; OKX contract volume uses captured
ctVal/ctMult/ctValCcy. Unknown/missing venue is unavailable, never a Bybit veto.

`leaderVenue` means largest observed absolute 500ms return, and `leaderAgeMs` is
the age of that observation. It is not exchange lead-lag arbitrage or a causal
claim about who moved first. OFI is a best-level snapshot proxy; within-snapshot
events are unobservable. Alignment requires both external venues with available
returns; disagreement is neutral, missing is unavailable. These definitions are
fixed before studying breakout and weak_level_rejection outcomes.

Public API references: [Binance official connector stream contract](https://github.com/binance/binance-futures-connector-python/blob/main/binance/websocket/um_futures/websocket_client.py),
[OKX book/trade/instrument documentation](https://www.okx.com/docs-v5/en/).
The implementation additionally records actual wire messages and instrument specs.

Maker shadow posts at most one bid and one ask candidate per symbol, no faster
than once per second, with a 100 USDT research notional normalized to instrument
rules. Queue is max(displayed quantity, configured maker queue fraction); no
cancellation-based depletion. Only later received publicTrade batches with correct
aggressor, exchange time, strict configured trade-through and sufficient volume
can fill. Partial virtual fills retain individual lots. Quote touch does not fill.
TTL: 2s; markouts: 100/500/1000ms, first available executable depth within +100ms;
missing depth/windows/transport censor rather than impute. Markout net includes
maker entry fee, taker liquidation fee and configured slippage. Gross liquidation
Pnl is distinct from realized spread capture; pure spread capture and exact queue
error remain null when unobservable. These are research liquidations, no orders.

## V3

Features and strategy geometry are frozen at first preparation, including economic
rejections. A detached risk preview records current size/costs without reserving
capital. No resampling after a better price. Primary labels use that structural
plan and the existing PaperBroker, fees, partial/runner and strategy management.
Entry availability is conservatively delayed 250ms, bounded horizon 180s, maximum
128 simultaneous virtual paths. Same-received-batch trades cannot fill entries or
maker exits retroactively. Epoch, depth, wall-clock discontinuity, capacity and
right censoring produce explicit missing outcomes. Staged adds require a complete
portfolio path and are excluded from standalone labels rather than treated as a
new position. Instrument, source sequence and cost/fill events remain attached.

The collector records local UTC at preparation. Monotonic clocks are confined to
their capture; splitting uses UTC intervals. Independent labels have no portfolio
PnL. Validation of the primary input chain and native/counterfactual label replay
must precede promotion. Supplemental cross-venue replay alone cannot certify labels.

Training candidates are fixed LogisticRegression+Ridge and CatBoost classifier+
regressor. The regression head ranks executable net R. Median/normalizer/missing
masks fit training only; sigmoid calibration fits a separate interval. Explicit
global windows, >=60s embargo and whole-episode purge apply across train/calibration/
validation; LOSO excludes the symbol from fitting and calibration. Test captures
are reserved outside development; a separate command writes a single test receipt
after model selection. At least 30 observations/classes per fitting/calibration
partition are required for baseline execution; this is a technical floor, not
sufficient evidence of edge. No model is trained from the current sparse capture.

The rank/veto adapter binds source, intent, model and expiry. It sees only already
eligible opportunities, then the ordinary admission rebuilds current economics
and risk before FIRE and rechecks forecast expiry. Default mode is shadow.
No adapter, model, maker playbook or registry risk enforcement is enabled in the app.

## Experiments

The external controller accepts only independent PaperBroker arms, one ordered
shared input/scanner source and exact source/config/runtime/dataset/split/model/
calibration/adapter hashes. Fixed guards: 30 USDT loss and equity drawdown each,
additional B drawdown 0, 90s finalization, no fallback/restart/extension. A silent
feed does not extend the common Start deadline. A 30–60m technical run requires
data/model/adapter/ledger gates; 8h additionally requires that paired run to pass.

The controller is a tested contract; the live paired arm/feed adapter and frozen
V3 inference deployment remain a separate integration gate. It is not a launcher
for an untrained B arm. W2.6 maker execution, W2.7 maker ML and W2.8 risk allocation
remain unimplemented until their prescribed independent economic evidence exists.
