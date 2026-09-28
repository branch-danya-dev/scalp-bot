# Active scalping, Paper and Bybit Demo

The launcher now defaults to `scalping-active-v1`, requested by the owner on
2026-09-28. The previous `trading-24h-v1` policy remains explicitly selectable.
Both use the existing ordinary strategy/scanner/execution pipeline; no ML or
N3/W2 research launch gate. This is an activity policy, not demonstrated positive
expectancy or a guarantee of any number of trades per hour.

| Policy | Previous v1 | Active v1 |
|---|---:|---:|
| Minimum 24h turnover for scanner attention | $150m | $50m |
| Candidate universe / working symbols / active ceiling | 30 / 6 / 12 | 60 / 12 / 12 |
| Minimum active-symbol residence | 600s | 180s |
| Net target/stop R:R minimum | 1.15 | 1.0 |
| Maximum winner cost share | 35% | 50% |
| Rearm delay (fresh causal setup still required) | 20s | 5s |
| No-follow-through: trend / rejection / breakout | 45 / 45 / 120s | 20 / 15 / 30s |

The scanner threshold is an attention filter; admission still checks executable
spread, L50/L1000 coherence/depth, slippage and all-in risk. Lower turnover is not
treated as proof of liquidity. No entry uses a made-up wider target to pass R:R.

For active rejection, confirmed absorption and a fresh price response can qualify
before the price crosses the midpoint of the forming one-minute candle. The
legacy midpoint requirement is retained in the previous profile. Countertrend
absorption/participation, opposed-flow veto, short reaction target and no-runner
rules remain. Breakouts still need causal acceptance and price response.

The final active participation check requires normalized executed notional and
trade intensity, without a second mandatory acceleration check. A fresh 5-second
burst can override low accumulated candle pace only when BOTH notional and trade
count rate are at least 1.25x their baselines; direction and price response remain
mandatory. Thin XPL-like flow and missing baselines remain blocked. Strategy-level
causal flow confirmation remains in force. These numbers are policy choices.

## Risk/liquidity corrections and limits

After quantity rounding, the trading RiskEngine now reprices the exact base
quantity against executable depth, recomputes fees and stressed stop loss, and
reduces quantity if necessary to satisfy the exact cash limits. The old notional
VWAP could describe a larger order than the exchange would receive.

Broken structural levels are omitted from reachable liquidity; the raw candle
detector cannot resurrect the exact same retired zone. Worked or freshly reclaimed
generations remain valid. Consumed isolated swings are also omitted. Final targets
remain inside the frozen opportunity budget and before opposing liquidity.

A partial exit is selected only when its complete lifecycle qualifies. Otherwise
an admissible full exit at the SAME market target may be selected. No target is
extended to manufacture payoff. A full exit can surrender unrealized partial gains.

Independent Decimal tests walk book levels and reconcile entry/exit cash, fees,
partial/full target payoff and stressed loss for both directions, two fee schedules
and coarse quantity steps. This checks arithmetic, not future liquidity or win rate.
`economics.breakEvenWinRateTargetOrStop` is explicitly a binary target/stop
illustration; failure exits, partial outcomes and funding require observed results.

Both profiles retain: $1000 Paper/reference equity; 86400 seconds; 0.5% structural
risk ($5); 1.25% all-in trade cap ($12.50); 2% portfolio risk ($20); 10x portfolio / 5x
position exposure; four positions/four pending entries; structural stops; exchange
clock, freshness and reconciliation. Larger actual Demo equity is displayed but
starting risk/exposure is capped to the $1000 reference. ML entry/veto/rank/size
authority is OFF. Session-loss, trade-count, expectancy and adaptive segment stops
are OFF. No mainnet execution.

Tradeable: `level_breakout`, `weak_level_rejection`, `trend_structure`.
`orderbook_density` remains evidence-only; incomplete `price_action_hypothesis`
beta remains OFF. This does not add an orderbook market-making or pips strategy.

Planning uses configured 0.055% taker / 0.02% maker rates and 1bps taker slippage.
Demo settlement uses actual fills/fees. Account-specific VIP/promotion rates are
not automatically imported into this profile; configured planning can therefore
overestimate costs. Current book depth cannot guarantee future stop liquidity.

## PowerShell, repository root

Check and start with separate, automatically named output folders. Do not reuse
an existing run folder for Start. The script prints the exact folder and UI URL.

```powershell
.\scripts\run-trading-24h.ps1 -Mode Paper -Action Check
.\scripts\run-trading-24h.ps1 -Mode Paper -Action Start
.\scripts\run-trading-24h.ps1 -Mode Demo -Action ConnectedCheck
.\scripts\run-trading-24h.ps1 -Mode Demo -Action Start
```

Demo uses the ignored local `.env.demo-paper.local`. Every Demo Start performs
the connected no-order preflight before granting order authority. Foreign
positions/orders cause a clear refusal, not cancellation. Demo orders/fills belong
to the actual Bybit Demo account; the independent comparison Paper ledger is local.

UI: Paper `http://127.0.0.1:8000/`, Demo `http://127.0.0.1:8011/`.
The UI stop button requests graceful finalization. For terminal control, paste the
exact output folder printed at startup when prompted:

```powershell
$run = Read-Host 'Run output folder'
.\scripts\run-trading-24h.ps1 -Action Stop -Output $run
# After summary.json reports finalized=true:
.\scripts\run-trading-24h.ps1 -Action Archive -Output $run
```

Results are under `data/scalping-active-v1/`: `profile.json`, `status.json`,
`summary.json`; Paper `sessions/`, Demo `capture/` and journal/equity artifacts.
The normal deadline is 86400 seconds; protected finalization can extend beyond it.
Use `-Profile trading-24h-v1` to select the previous thresholds explicitly.
