# Frozen diagnostic protocol (before new outcome calculations)

Base: 1d7d9aba6c378cba70403d10234b26d9f7df367e. Local/remote match, clean worktree; Python 3.13.15 and scalp_bot import from this worktree. Use existing A/B/C ledgers/events, complete raw quote index and canonical dataset-v2-verified/model-v2. No new replay of portfolio or training. All outputs in docs/pr58-trade-plan and G:/scalp-bot/data/audit-pr58-trade-plan. Original artifacts read-only.

## Ordinary bot

Names: single_legacy / parallel_legacy / parallel_quote_tape. Exact matching uses symbol/owner/object/episode/side; never match by symbol alone. One row per execution variant, grouped execution identity for cross-variant comparison; changed fill/quantity/plan explicitly retained. Attribute A-to-B delta by exact retained/new/disappeared keys; arithmetic residual must be <1e-8. Ambiguous economic identity retained separately.

Evidence: first ready, admission, opened/closed, actual ledger fees/funding/legs, decision context/flow/regime/level, original plan and actual management. Market waiting is distinct from ready-to-order and order-to-fill. Top-of-book horizon diagnostics 5/15/30/60/120 seconds from ready and fill use the existing complete quote index with causal sequence fence, 1.5s gap guard and first passage stop/target ordering. These are bounds, not new fills or portfolio PnL; never subtract spread/slippage again from ledger. Individual fees and actual fills reconcile cash. Missing stages remain unknown.

Control groups fixed before outcomes: all reconstructed ready episodes, by owner, side, routing effect retained/new/disappeared, and known/unknown quote coverage. Retain favorable controls and original T01-T06 broker results; don't cherry-pick WAIT events. Assign main cause manually using causal evidence plus path, not sign alone: unconfirmed direction/reversal; late entry; insufficient cost-paying scale; incoherent plan; disputed management; unreliable data; confirmed defect; insufficient evidence. Positive controls kept explicitly.

## ML V2 (no test analysis or fitting)

Only train/calibration/validation rows enter new diagnostics. Verify immutable hashes and frozen 30/15bps/30s/100ms/100USDT/0.055%/1bp policy. Summaries by split, symbol, side, capture, class; pre-state groups (separate overlapping partitions): liquidity known vs unknown; signed 5s move <=0 / (0,2) / >=2bps / unknown; signed 5s trade imbalance <=0 / (0,.2) / >=.2 / unknown; forming range <15 / [15,30) / >=30bps / unknown; spread <1 / [1,3) / >=3bps / unknown; top5 depth <1000 / [1000,10000) / >=10000USDT / unknown. Do not change bins after results.

Class payouts include gross, fees, net, positive/negative timeout, durations and original barrier distances after tick rounding. Quote-path MFE/MAE is separate raw replay diagnostic if available; absence cannot be inferred from endpoint alone. Exclude test and purged records from new calculations. Check real payoff identities and mirrored synthetic PendingLabel fills. Fixed time groups are capture/global 60s bucket (dependence, not independence).

Validation ranking: existing calibrated p_target of CatBoost and logistic; existing fixed rule and train prior; no-trade cash=0. Ten equal-count ascending ranks (stable input tie order), overlapping top1/5/10 percent, and unchanged >=.55 control. Report n, group count, class rates, mean/median/p05/p95 net, positive timeouts, per-symbol concentration, leave-one-time-group-out mean range and group bootstrap 95% interval (1000 resamples, seed1729). Tied prior/rule ranks have no within-tie predictive meaning; state this explicitly. No score is equated to expected net, no policy chosen from test, no overlapping payout sum called portfolio PnL. At most one hypothesis for separate approval after analysis.

## Isolated performance

First unchanged harness: same offsets120/180s, 60s windows, models v1/v2 respectively, source hashes checked against prior reports. 1x/4x, 3 pairs each, alternating off/shadow. No concurrent own training/replay/tests/indexing/download. Preserve recorder and all inputs, freshness, stale results, original 20/5/250ms budgets. Save OS/process pressure and failures. Then stage instrumentation or minimal proven fix, full same suite again if changed. Explicitly separate scheduled delivery, generator, enqueue/process/state/strategy/features/worker/predict/return/adapter with linked IDs and clocks. No queue timestamp reset. Local logical-clock replay is not external network certification.

PR59 protocol read separately by immutable remote reference; remains 28800s, positive ordinary net and conditional hybrid utility, launch_authorized=false, selected_mode=null. No edits or launch.
