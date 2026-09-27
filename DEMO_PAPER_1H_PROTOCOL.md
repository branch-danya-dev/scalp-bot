# demo-paper-execution-1h-v1

Status: implemented for local verification; owner-operated Demo connection and measured hour have not occurred. Default off. This protocol is independent of PR59 and `next-paper-8h-v1`.

## Question and fixed observation

Compare the monetary execution of the same immutable trading intentions in Bybit Demo and a local paper venue. These are two simulators; neither result establishes live matching, competition or profitability. A better Demo outcome must be attributed to observed fills, fees, rejects, timing or subsequent management commands, not merely to its final sign.

One monotonic Start opens a 3600-second observation interval. Bootstrap, pauses, lack of signals and worker outages consume it. No restart, extension to first trade, artificial market signals or extension to profit. Stop cancels experiment entry remainders, replaces owned resting exits with reduce-only market closure and allows at most 90 seconds for reconciliation. Two final flat snapshots and order/execution reconciliation are required. Unknown fees/funding or unresolved exposure remain incomplete.

Zero opportunities is a valid observation but does not test the execution hypothesis. No model fills means `ML_TRADING_NOT_TESTED`; forecasts are not trades. Early stop retains every loss and unmatched execution and is `INCOMPLETE`.

## Admission and ownership

One existing TradingEngine scanner, market transport, MarketContext and ordinary parallel preparation feed the existing RiskEngine/arbiter. The opt-in execution boundary receives its approved TradePlan before either venue executes. Production methods default to the original paper path.

The intent freezes plan JSON, pair ID, exact source input-journal sequence, original receipt monotonic time, author and model version. Both bounded entry queues receive the same quantity in one non-awaiting reservation operation. Slow Demo REST cannot block market processing, paper dispatch or model-independent strategies. Per-arm management tasks are separate as well.

Research equity is 1000 USDT per arm. Rule and ML share each arm's budget; ML has no extra deposit. Sizing uses the lower research balance and shared reserved risk/exposure, not Demo wallet equity. The original B/parallel_legacy configuration in `docs/pr58-readiness/run-provenance.json` is frozen in the passport: risk fraction .005, maximum all-in trade loss .0125, total risk .02, aggregate leverage cap 10, position leverage cap 5, four simultaneous reservations. Leverage on the exchange is only read, never changed.

A symbol stays reserved until both arms are flat, all its orders terminal and execution quantities reconciled. This deliberately also prevents staged additional intents while a pair is occupied; it is an experiment matching constraint, not a new production strategy rule. The current ordinary profile already disables breakout/rejection staged additions. Other symbols and ordinary preparation continue. Unmatched fills/rejections remain in the table; no catch-up entry is sent.

## Frozen exit and paper semantics

`local-executable-bid-ask-with-resting-maker-limits-v1`: protective stops, market targets and timeout are evaluated locally on executable bid/ask. No native LastPrice/MarkPrice/IndexPrice stop is installed or mislabeled equivalent. Local protection therefore requires a functioning controller; process/power loss cannot guarantee liquidation and must not be called a successful run.

The common `Arm.desired` management function operates on each arm's actual weighted entry, remaining size, costs and partial state. It reuses PaperBroker's strategy progress policy, partial price/economic checks, fractions and extracted unchanged runner management. Strategy invalidation receives only that strategy's own position. After actual fills diverge, different management commands are recorded as command differences.

Rule maker partial/target orders are explicit resting reduce-only PostOnly limits created after entry. Their paper confirmation requires correct-side trade-through/queue notional received after order creation or amendment. A stale batch cannot fill a newly created exit. PostOnly crossing rejects are retained, not converted to market fills. The paper venue uses existing depth VWAP, configured .0002 maker/.00055 taker fees, 1 bp taker slippage and queue assumptions. Its explicit IOC model can fill visible partial quantity and cancel the remainder; reduce-only matching cannot reverse. This research order venue is separately versioned by this protocol and is not a claim of byte-identical legacy PaperBroker output.

Partial quantities are rounded down to the verified lot step equally in both arms. Protective commands are canceled and reconciled before replacement after fills/geometry change. A late entry fill is still attached to its original pair and receives protection. A failed protective order halts entries and invokes bounded termination.

ML retains V2 weights, 10-second observation grid, 60-second coverage warmup, both sides, threshold .55, nominal 100 USDT, 30/15 bps and no partial. As in the labels, each arm's actual weighted fill anchors its 30/15 bps geometry; the 30-second deadline remains anchored to the original observation. Quotes/drift/TTL, epoch, schema and model version are checked before admission and again before a queued Demo entry is sent. No timestamp refresh, first-ready sampling experiment or V3 is introduced. Actual send/fill receipt delay is recorded; the old 100 ms label assumption is not promised.

## Destinations and account

Private REST is strictly `https://api-demo.bybit.com`; private WS is strictly `wss://stream-demo.bybit.com/v5/private`. Only create/cancel/amend are writable. No cancel-all, transfers, funding requests, account/key creation, leverage change or private mainnet fallback exists. Public REST/WS are mainnet read-only, with empty credentials and no redirects/fallback hosts. Demo order dispatch uses REST; private order/execution/position plus paginated REST establish outcomes.

The ignored `.env.demo-paper.local` must contain an owner-created Demo Trading key/secret and expected Demo UID. No key is requested in chat, passed on the command line, included in recorder input envelopes or sent to the predictor. Windows-spawn environment is scrubbed of API credential variables. The worker accepts immutable feature messages only; the research adapter is separate from unchanged `ml/shadow.py`.

Authenticated no-order preflight verifies the expected masked account identity, trading permissions, supported unified account, absence of foreign orders/positions across linear USDT, linear USDC, inverse, options and spot orders, and mandatory endpoints. The same account scopes are inspected during REST reconciliation after a private gap. Per-symbol bootstrap additionally verifies a zero position in one-way mode, records actual exchange leverage and fetches mandatory current linear-USDT-perpetual instrument fields. Unknown tick, qty step, min qty, min notional or maximum size blocks admission. No historical 2024 specification is inferred from this snapshot. Foreign activity detected later halts the experiment and is never canceled/closed on its behalf.

## Lifecycle and accounting

POST acknowledgement is separate from execution. Unknown create response is reconciled by the same deterministic <=36-character orderLinkId; no create retry with a new ID is used. Execution IDs are deduplicated across WS/REST and checked for conflicting ownership/content. Partial quantities, weighted prices, actual Demo fees/currency and paper fixed fees are separate. Cancel acknowledgements never stand in for terminal state. Private gaps close admission and permit Demo-only reconciliation/termination; they do not trigger a mainnet fallback.

Funding uses signed private transaction-log settlement records once per transaction ID. Paper retains the original ticker-based estimate, explicitly labeled estimated. An observed funding boundary with no private settlement confirmation is unknown, not zero. Unsupported fee metadata halts admission, quarantines monetary results and preserves validated physical quantity for reduce-only termination. The internal accounting placeholder in that emergency path is never published as a confirmed zero fee/net result.

Research ledgers and actual Demo wallet snapshot are separate. Wallet collateral revaluation is not trading PnL. Actual Demo fill prices receive no second paper slippage deduction. Actual and fee-normalized diagnostics are separate, with fees, funding and final closure counted once.

## Bounds and required output

Frozen passport: 30 USDT session loss bound per arm including current marked exposure; 32-entry queue per arm; 5-second private REST timeout; 30-second maximum private snapshot age; 90-second reconciliation bound. Existing 20/5/250 ms loop/added-shadow/adapter budgets are retained. A single paired run has no off counterfactual, so it cannot certify added-shadow latency; the old PR58 benchmark is scoped historical evidence only.

Output directory is unique and cannot be overwritten. Raw input journal/production recorder stays enabled. Any writer error/drop halts the run. Files include every pair/command/fill/rejection, actual and fee-normalized net, unmatched cases, changed commands/occupancy, source/features/predict/admission/send/ack/fill receipt stages, original exchange execution wall timestamps, model decision funnel, instrument provenance and checksum manifest. Unknown final funding/net is blank; observed settlement cash is a separate field. The send timestamp begins after the private REST queue/pacing wait. Calibration manifest identity is pinned alongside weights; delivered rejected forecasts remain in the funnel. Local monotonic receipt clocks are not subtracted from exchange wall clocks.

## Owner procedure

From a visible PowerShell in the worktree:

```powershell
.\scripts\run-demo-paper-1h.ps1 -Action Preflight
```

One local setup step: copy `.env.demo-paper.example` to ignored `.env.demo-paper.local`, fill the Demo-only key, secret and expected UID locally. Never commit or paste them. An optional `-Action ConnectedPreflight` performs authenticated reads without orders.

Review the displayed profile, hash, numerical bounds and output directory. The separate explicit Start command is:

```powershell
.\scripts\run-demo-paper-1h.ps1 -Action Start -ConfirmPassport c1fffddfa76ab234bb0eda6d54225a04ecda2a3f1d10da5a0d34108c585beab3
```

This command is prepared, not executed by this implementation task. Ctrl+C/termination signals stop admission and enter reconciliation. An incomplete run requires inspecting retained unknown exposure and the Demo account; the program never restarts it automatically.

## Official sources checked during implementation

- [Demo service/endpoints](https://bybit-exchange.github.io/docs/v5/demo): isolated Demo destinations and supported REST/private streams; no WS Trade.
- [Demo limitations](https://www.bybit.com/en/help-center/article/FAQ-Demo-Trading): simulated account, not evidence of live matching.
- [Create order](https://bybit-exchange.github.io/docs/v5/order/create-order): acknowledgement, PostOnly, IOC and reduce-only semantics.
- [Execution stream](https://bybit-exchange.github.io/docs/v5/websocket/private/execution) and [execution history](https://bybit-exchange.github.io/docs/v5/order/execution): execution identities, quantity, price and fee currency.
- [Position info](https://bybit-exchange.github.io/docs/v5/position): one-way/hedge identities and symbol-scoped queries.
- [Transaction log](https://bybit-exchange.github.io/docs/v5/account/transaction-log): positive funding means received cash; delayed settlement records remain possible.
- [Native trading stop](https://bybit-exchange.github.io/docs/v5/position/trading-stop): read for trigger incompatibility; this protocol does not use that endpoint.
