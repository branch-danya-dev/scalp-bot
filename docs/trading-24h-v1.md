# Ordinary non-ML trading, 24h v1

This profile starts at PR61 `39713333c5342d272649c142c7f66a1237c0ddaa`, stacked on PR60
`1a2d0f67da0331df7d2d134bffde6f64141c7898`. No merge or base-branch change.
N3/W2 replay evidence and its NOT_MET status remain historical evidence, not a launch gate.

## Trading policy

| Module | Profile role |
|---|---|
| `level_breakout` | Tradeable; frozen remaining move, normalized participation, local countertrend veto, strong obstacle veto, reachable target |
| `weak_level_rejection` | Tradeable; trend-aligned / range / countertrend reaction classification |
| `trend_structure` | Tradeable; parent trend continuation only, existing pullback/test/reclaim/response and structural invalidation lifecycle |
| `orderbook_density` | Evidence only; standalone execution was removed before this change |
| `price_action_hypothesis` | Off; explicitly opt-in paper beta, generic inherited management, no qualified Demo lifecycle |
| ML V2 / V3 | No worker, model, ranker, collector, entry generation, veto or sizing influence |

The optional settings switch `trading_quality_enabled` is off in historical profiles.
`scalp_bot.trading24h.settings()` constructs every setting explicitly, ignoring inherited
SCALP variables and `.env`. Public requests carry no credentials. Private writes are
restricted to the existing Bybit Demo adapter; redirects and mainnet private hosts are forbidden.

At each economic plan, including the final FIRE check, the policy uses the scenario's
causal price episode, its frozen expected impulse and the actual executable entry.
`remaining = max(frozen distance - max(directional spent distance, 0), 0)`.
Target is capped by remaining movement, nearest liquidity and the near edge of a mature
foreign obstacle. An obstacle before first take blocks breakout admission; the accepted
breakout's own level cannot conceal the next foreign obstacle. Neither risk nor post-partial
management extends these targets to satisfy R multiples. The existing `1h_only` override
and fast failed-break/acceptance-loss exits remain in force.

Participation requires a 5-second executed-notional ratio against the symbol's recent
20 closed one-minute candles, local notional acceleration, trade count/rate, directional
imbalance and price response. A known forming-candle volume pace below 0.75 blocks admission.
No absolute USD participation threshold is used. A missing baseline is not confirmation.
The 0.75 ratios and 1bps response floor are conservative policy choices, not fitted optima.

Countertrend reaction requires absorption plus immediate response and participation;
it has no partial/extended runner, at most half the frozen scenario movement budget,
20-second no-follow-through policy and 1-second zone failure persistence. Continuing
opposed Bybit flow can block/exit it. Trend-aligned rejection retains partial/BE/runner
within its market target. No assumed optimal 0.25–0.35 risk scale or 5–15-second timeout.
Sweep invalidation uses the more distant zone/round boundary or observed episode sweep
extreme plus buffer. RiskEngine recalculates quantity; wide or uneconomic stops reject.
Same-symbol selection uses alignment, reachable net reward/risk, external agreement and
setup quality before the original deterministic ready order.

CrossVenue uses the existing public Binance/OKX service without the research coordinator.
Both venues must be fresh and agree in 1-second price movement and executed flow for
`aligned`/`opposed`. Missing/stale data yields `unavailable`, with neutral admission effect.
Fresh opposition vetoes new breakout/countertrend reaction; agreement affects selection.
External venues never manage positions or stops.

## Risk and execution

Paper starts at 1000 USDT. Both modes use 86400 monotonic seconds including startup,
reconnects and pauses; graceful position finalization may continue after the deadline.
No new Demo entry may be sent after the deadline.

Reference risk: 0.5% structural ($5), 1.25% all-in per trade ($12.50), 2% portfolio risk
($20), 10x gross portfolio ($10000), 5x per position ($5000), four positions and four
pending entries. These are ceilings, not target position sizes. Net RR 1.15 is a safety
policy margin, not a proven optimum; hard floor 1.0 and winner-cost-share 0.35 are on.
The fixed/equity-based $1 minimum-profit hard gate is off: actual lifecycle costs,
positive payout, RR and cost-share already constrain admission, without rejecting a
smaller viable scalp solely because it earns less than $1.

Demo reads and displays actual USDT equity. Starting risk/exposure fractions are scaled
by `min(actual USDT equity,1000)/actual USDT equity`; the ledger retains actual equity.
Thus a 50000-USDT Demo wallet still has at most the same initial $5 structural risk and
$5000 single-position exposure as the $1000 Paper profile. Equity below $1000 reduces
dollar risk proportionally. Other collateral is not counted as USDT risk capital.
An equity change between preflight and start requires a fresh preflight.

Paper uses configured 0.055% taker / 0.02% maker fees, slippage, coherent L50/L1000 depth
and maker tape/queue confirmation. Demo books actual executions and fees. Demo mode
reuses the existing paired execution architecture: one scanner/decision stream, a real
Demo ledger and an independent comparison Paper ledger; no copying Demo fills to Paper.
The comparison ledger starts at the same actual equity and uses the same scaled dollar
limits. It is not a second process or an ML arm.

Removed profile limits: session/daily loss stop, paired-arm $30 drawdown stop,
trade-count cap, mid-run strategy expectancy/adaptive segment disabling, ML veto and
N3/W2/900-second qualification gates. Preserved: structural stops, all-in sizing,
position/portfolio exposure, leverage/concurrency, coherent/fresh book and clock checks,
reduce-only orders, strict ownership and reconciliation.

## Runtime behavior

Public/private WS gaps, missing CrossVenue, clock invalidity, empty scanner results and
temporary REST failures pause necessary new admission while background recovery continues.
Empty startup rescans instead of exiting. Private application heartbeat is unchanged.
GET retries are bounded; uncertain mutations are resolved by their owned order ID, never
blindly repeated. A local pre-send refusal is recorded as known not sent, and a queued
entry skipped during a pause releases its reservation after reconciliation.

Demo protection is managed locally with Bybit reduce-only orders, as in the existing
adapter. An exposure data/reconciliation gap lasting 20 seconds requests owned emergency
liquidation; inability to protect/reconcile within 90 seconds triggers terminal safe
shutdown/finalization. This is a safety recovery bound, not an experiment loss stop.
Foreign orders/positions are never silently cancelled. Do not share the Demo account
with another bot or manual trading during the run.

Shutdown stops admission, cancels bot-owned pending orders, finalizes owned residual
exposure, reconciles and writes `summary.json`. A missing/false `finalized` flag is an
incomplete shutdown, never reported as successful. Ctrl+C is graceful; forcibly killing
the process or powering off the computer cannot provide local position management.

## Windows commands (repo root)

Use only a revision whose full preflight, exact-head CI, launcher Check, public Paper
smoke and Demo no-order checks have been reviewed. The launcher's Demo Start always
repeats connected no-order preflight before enabling any writes.
Dependencies: Python 3.12+ and `.venv` with `pip install -e ".[dev]"`; no ML dependency
or training is needed to run this profile. `.env.demo-paper.local` uses the existing
`.env.demo-paper.example` format; keep secrets local and out of terminal arguments.

```powershell
# PAPER CHECK
.\scripts\run-trading-24h.ps1 -Mode Paper -Check
# PAPER START (86400 seconds)
.\scripts\run-trading-24h.ps1 -Mode Paper -Action Start -Output data\trading-24h-v1\paper-run -Port 8000
# DEMO CHECK / connected NO-ORDER preflight
.\scripts\run-trading-24h.ps1 -Mode Demo -Action ConnectedCheck -Output data\trading-24h-v1\demo-check
# DEMO START, with the existing independent comparison Paper ledger
.\scripts\run-trading-24h.ps1 -Mode Demo -Action Start -Output data\trading-24h-v1\demo-run -Port 8011
# Graceful STOP from another terminal (or Ctrl+C in the run terminal)
.\scripts\run-trading-24h.ps1 -Action Stop -Output data\trading-24h-v1\paper-run
.\scripts\run-trading-24h.ps1 -Mode Demo -Action Stop -Output data\trading-24h-v1\demo-run
# Archive after summary.json says finalized=true
.\scripts\run-trading-24h.ps1 -Action Archive -Output data\trading-24h-v1\paper-run
.\scripts\run-trading-24h.ps1 -Mode Demo -Action Archive -Output data\trading-24h-v1\demo-run
```

Use a new output folder for every start. No auto-restart loop is installed.
The original Scalp Bot terminal is shared with the ordinary app: charts, order book,
scanner, strategies, decisions, positions, trade history and existing session/review APIs.
Paper defaults to `http://127.0.0.1:8000/`; Demo to `http://127.0.0.1:8011/`.
The badge identifies PAPER / BYBIT DEMO. Demo balances, positions and fills come from
its actual ledger; comparison Paper data is separate. The timer uses the existing
coordinator deadline. UI Stop requests full graceful finalization; UI Start cannot
reset the 24h deadline and strategy toggles are locked to the selected profile.
An occupied UI port fails before the new runtime or any Demo write starts.
Omitting `-Output` selects a fresh timestamped folder. Running processes need a graceful
restart to load this UI change; existing results remain in their original directories.
Outputs: `profile.json`, live `status.json`, final `summary.json`, Paper `sessions/`
or Demo `capture/` JSONL logs. Admission details include `remainingMove`,
`participationQuality`, `rejectionClass`, `crossVenueContext`, sweep/stop source and
economic rejection reason. Keep the run directory for diagnostics and post-run analysis.

Regression suite includes 24h deadline, operator stop, queued-not-sent recovery,
private/public gaps, clock pause/resume, empty-universe rescan, REST read retry,
independent Demo/Paper fills and reconciliation. A short live smoke proves startup
and stream health; it does not prove 24-hour uptime or profitability.

Bybit reference: [Demo destinations](https://bybit-exchange.github.io/docs/v5/demo),
[private heartbeat](https://bybit-exchange.github.io/docs/v5/ws/connect).
