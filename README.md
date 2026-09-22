# Scalp Bot — Strategy Logic v3 · 4h Paper Run

This run branch is the prepared Strategy Logic v3 research state for a controlled four-hour paper run.

## Strategies in this run

1. Trend structure.
2. Horizontal traded-zone bounce.
3. Weak-level rejection:
   - young level with 1-3 approaches;
   - no prolonged acceptance around the zone;
   - failed breakout/reclaim;
   - trade-flow reversal;
   - round-number confluence;
   - trend-following reactions may use a runner;
   - countertrend reactions are reaction-only.
4. Defended fresh order-book density:
   - large bid/ask wall relative to local book;
   - persistence required before trust;
   - real approach/test required;
   - repeated approaches, strong depletion or aggressive consumption invalidate the bounce;
   - pulled walls are not traded;
   - entry requires defended wall + flow reversal;
   - trend-following density reactions may use a runner;
   - countertrend density reactions are reaction-only.
5. Stateful horizontal-zone breakout:
   - SEARCH -> FOUND -> APPROACH -> PRESSURE -> BREAK -> IMPULSE;
   - zone crossing plus public-trade-flow confirmation.

## Shared trading lifecycle

- one setup = one trade;
- consumed setup must reset/rearm before another entry;
- sticky active symbols;
- central opportunity arbiter;
- stale-market protection;
- expected net profit + net reward/risk gate;
- 70% partial at about 1R when runner is allowed;
- 30% runner -> estimated net breakeven;
- runner target about 2.5R;
- no-follow-through early cutting;
- structural invalidation before emergency hard stop;
- paper positions finalize on Stop, shutdown, or the 4h deadline;
- one position may use at most 25% of portfolio exposure by default, so a tight scalp stop cannot monopolize all capital.

## Research-run rules

There is deliberately **no cumulative session-loss kill switch**:

```
SCALP_ENFORCE_SESSION_LOSS_LIMIT=false
```

Per-trade and simultaneous portfolio risk controls remain enabled.

Research cost gate:
- a setup must remain net-positive after estimated fees, spread and slippage;
- minimum expected net is $0.10 by default;
- net reward/risk is recorded for analysis but the live-style RR>=1.15 gate is disabled during research, because it mathematically suppresses most tight-stop scalp setups.

## 4-hour harness

The timer starts after pressing Start.

The launcher forces the run profile even if an older local `.env` is present:

```
SCALP_RUN_LABEL=paper-v3-4h
SCALP_PAPER_RUN_DURATION_SECONDS=14400
```

At the deadline:

- no new positions;
- remaining paper positions are closed;
- trade_closed events are recorded;
- run_summary is written;
- server remains available for Replay.

REST resilience:
- all Bybit REST requests are globally paced;
- HTTP 429 / Bybit 10006 use exponential retry/backoff;
- an exhausted temporary rate limit no longer crashes FastAPI startup;
- failed symbol bootstrap is skipped and retried by subsequent scans.

Replay sampling:
- 1 second while a setup/position is engaged;
- 5 seconds during idle observation;
- important decisions/trades carry event snapshots.

## Windows update

```powershell
git fetch origin
git switch paper-run-v3-4h
git pull origin paper-run-v3-4h

.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env -Force
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Before Start, verify the UI says:

```
RESEARCH · LOSS CAP OFF
```

and that the strategy list contains both:

```
Отбой от слабого уровня
Отскок от свежей плотности
```


## Strategy Logic v3 architecture

Trading logic is now modular:

- strategy/trend_structure.py
- strategy/weak_level_rejection.py
- strategy/density.py
- strategy/breakout.py
- strategy/common.py for shared market primitives only

The former strategies.py is only a compatibility export shim.

Long-run findings applied in this patch:
- weak-level support and resistance paths are symmetric and tested;
- psychological round-number confluence is intentionally rare instead of nearly universal;
- countertrend reactions use a shorter 0.75R target and never create a runner;
- density tracks wall persistence, depletion, replenishment and absorption over time;
- removal of a wall after a confirmed bounce is not by itself an exit signal;
- density invalidation requires adverse price acceptance plus aggressive flow through the old wall;
- breakout zones require at least 5 touches plus reaction/volume maturity;
- one breakout zone generation can produce only one trade;
- arbiter ranks setup quality, not deterministic net R/R geometry;
- paper stop/target/partial triggers use executable bid/ask;
- run_summary keeps a lifetime closed-trade counter even though the UI history is capped.

## Market activity and liquidity targets

Candidate selection now carries a market-activity profile:
- verified Bybit 24h turnover;
- verified Bybit 24h base volume;
- verified 24h price change;
- 5m turnover burst and price activity;
- 1h Pearson correlation of aligned 1m returns versus BTCUSDT;
- an interpretable activity score used as a secondary arbiter input.

tradeCount24h exists as an optional external metric, but Bybit V5 tickers do not publish it and recent-trade REST is capped, so the bot deliberately does not fabricate a 24h trade count. A screener/provider can populate it later.

Levels are explicit liquidity targets. strategy/liquidity.py searches in the direction of the trade for the nearest meaningful pool: a repeated horizontal zone or an isolated external swing high/low. Trend-following strategies may target that pool; countertrend reactions remain capped.

## Central Level Engine

All strategies now receive one shared MarketStructure instead of detecting important levels independently.

The engine builds:
- 1m horizontal zones;
- direct 5m horizontal zones from dedicated history;
- 15m horizontal zones;
- direct 1h zones from dedicated history;
- current and previous UTC-day high and low;
- scored diagonal support/resistance lines from repeated pivots;
- round-number confluence as a secondary property, not as a level by itself.

Overlapping levels from different timeframes are deduplicated and receive a multi-timeframe strength bonus. Breakout uses mature shared levels, weak rejection uses young 1-3 touch shared levels, trend structure prefers the shared diagonal line, and liquidity targeting can use day high/day low as external stop-pool hypotheses.

Important: these are inferred liquidity areas. The exchange does not expose other traders' stop orders, so the bot treats stops beyond highs/lows/levels as a hypothesis supported by market structure, not as directly observed orders.

Density decisions also expose amount, distance, lifetime, erosion duration and round-number confluence so we can analyze the same dimensions that specialist screeners expose publicly.


## Order-book hardening in this run

- Bybit depth 1000 is used for density research.
- Local order-book state must be synchronized before density is evaluated.
- Sequence gaps clear the local book and require a fresh snapshot.
- Stale or unsynchronized books cannot open new positions.
- Density records actual bid/ask percentage coverage instead of assuming a fixed range from a level count.
- A wall outside the currently observable book is treated as unknown, not as removed.
- Wall significance requires all of: an absolute USD floor, a local-neighbor relative multiple, and an activity-scaled turnover floor.
- Density confirmation uses trade flow and absorption at the wall itself.

## Prepared run

Branch:

```
paper-run-v3-4h
```

Start it from a visible PowerShell window with:

```powershell
git fetch origin
git switch paper-run-v3-4h
git pull origin paper-run-v3-4h
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

The script runs the full preflight test suite before starting Uvicorn. Press **Start** in the UI only after the server is up; the four-hour auto-stop timer begins at that point.
