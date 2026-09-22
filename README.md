# Scalp Bot — Strategy Logic v3 · Scalp Economics 4h Paper Run

The main branch is the prepared Strategy Logic v3 state for the next controlled paper run with the revised scalp position-sizing, execution, and economic model.

## Strategies in this run

1. Trend structure.
2. Weak-level rejection:
   - young level with 1-3 approaches;
   - no prolonged acceptance around the zone;
   - failed breakout/reclaim;
   - trade-flow reversal;
   - round-number confluence;
   - entries are allowed only when the rejection direction agrees with the confirmed higher-timeframe trend;
   - trend-following reactions may use a runner.
3. Defended fresh order-book density:
   - large bid/ask wall relative to local book;
   - persistence required before trust;
   - real approach/test required;
   - repeated approaches, strong depletion or aggressive consumption invalidate the bounce;
   - pulled walls are not traded;
   - entry requires defended wall + flow reversal;
   - entries are allowed only when the defended-wall reaction agrees with the confirmed higher-timeframe trend;
   - trend-following density reactions may use a runner.
4. Stateful horizontal-zone breakout:
   - SEARCH -> FOUND -> APPROACH -> PRESSURE -> BREAK -> IMPULSE;
   - zone crossing plus public-trade-flow confirmation.

## Shared trading lifecycle

- one setup = one trade;
- consumed setup must reset/rearm before another entry;
- sticky active symbols;
- central opportunity arbiter;
- stale-market protection;
- equity-scaled economic gate after fees/slippage;
- dynamic notional from structural stop distance;
- partial requires both >=1R and an economically positive closed leg;
- 30% runner -> true net breakeven after remaining costs;
- structural liquidity targets remain binding after a partial; only fallback/non-structural targets may extend toward the configured runner R;
- no-follow-through early cutting;
- structural invalidation before emergency hard stop;
- paper positions finalize on Stop, shutdown, or the 4h deadline;
- one position may use up to 5x equity when the structural stop is tight enough;
- aggregate gross portfolio exposure is capped at 10x equity;
- aggregate open all-in risk (structural risk + reserved costs) is capped at 2% equity.

## Research-run rules

There is deliberately **no cumulative session-loss kill switch**:

```
SCALP_ENFORCE_SESSION_LOSS_LIMIT=false
```

Per-trade and simultaneous portfolio risk controls remain enabled.

Research economics:
- a trade must remain net-positive after estimated taker fees and slippage, with spread represented by executable bid/ask pricing rather than subtracted twice;
- $1 / 0.1% minimum-net is a shadow diagnostic, not a hard research gate;
- net reward / all-in net loss 1.15 is a shadow diagnostic, not a hard research gate;
- every shadow breach is recorded as `economic_shadow` for later Trade Observatory analysis;
- existing strategies trade only in the direction of the confirmed higher-timeframe trend; countertrend reactions are observed but not opened.

## Paper-run harness

The timer starts after pressing Start.

The launcher forces the run profile even if an older local `.env` is present:

```
SCALP_RUN_LABEL=paper-v3-scalp-econ-4h
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
git switch main
git pull --ff-only origin main

.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env -Force
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

For the prepared one-hour diagnostic research run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-research-1h.ps1
```

This loads `.env.example` first and then overrides only the run label and duration from `.env.research-1h`. See `docs/research-run-1h.md`.

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
- weak-level rejection and defended-density entries are trend-aligned; countertrend entries are not traded in the current policy;
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
- current best bid/ask spread and conservative top-of-book notional from the Bybit ticker;
- an interpretable activity score used as a secondary arbiter input.

The initial universe rejects symbols whose ticker spread alone would make the best executable quote violate the configured entry-drift limit. Full depth is still checked again at the actual entry.

tradeCount24h exists as an optional external metric, but Bybit V5 tickers do not publish it and recent-trade REST is capped, so the bot deliberately does not fabricate a 24h trade count. A screener/provider can populate it later.

Levels are explicit liquidity targets. strategy/liquidity.py searches in the direction of the trade for the nearest meaningful pool: a repeated horizontal zone or an isolated external swing high/low. The current trading policy only opens trend-aligned entries.

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
- Paper entries walk visible order-book depth and use depth VWAP before the configured additional slippage reserve.
- Research recording is adaptive: idle frames stay compact, while an engaged density setup records the full configured order-book depth so the decision can be replayed.

## Prepared run

Branch:

```
main
```

Start it from a visible PowerShell window with:

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

The script runs the full preflight test suite before starting Uvicorn. Press **Start** in the UI only after the server is up; the four-hour auto-stop timer begins at that point.


## Scalp economics in this run

The paper account starts at $1,000.

Position sizing is derived from structural invalidation distance:

```
structural risk budget per trade = 0.5% of equity
theoretical notional = structural risk budget / stop distance
```

Trading costs no longer silently reduce that structural budget. They are constrained separately by:

```
max planned all-in loss per trade = 1.25% equity
max aggregate all-in open risk = 2% equity
max single-position leverage = 5x equity
max aggregate portfolio leverage = 10x equity
```

For research, the hard economic requirement is:

```
net at configured target > 0 after estimated trading costs
```

The legacy `$1 / 0.1% equity` minimum-net and `net R:R >= 1.15` checks remain visible as shadow diagnostics and are recorded without blocking an otherwise valid paper entry.

The deterministic cost estimate includes taker fees and configured slippage. Entry planning and paper fills use executable bid/ask plus visible-depth VWAP, so spread and depth impact are represented by executable prices rather than subtracted a second time.

For runner-enabled setups, the legacy "take 70% exactly at 1R" rule is replaced by:

```
MFE >= 1R
AND
estimated net of the 70% closing leg >= required net threshold
```

After the partial, the runner stop is calculated from remaining entry fee, exit fee, exit slippage and the breakeven buffer so the remaining leg is protected at actual net breakeven. If the strategy target came from a detected liquidity pool, that structural target is preserved instead of being pushed mechanically to 2.5R.

The run label for this exact economics revision is:

```
paper-v3-scalp-econ-4h
```


## Trade Observatory

The observability/debugging layer is documented in:

```
docs/trade-observatory.md
```

It provides:

- normalized strategy Decision Trace;
- interactive Trade Review Cards for closed trades;
- chart timeframes 5s / 15s / 1m / 5m / 10m / 15m / 1h;
- Active / HTF / All level filters;
- DOM interpretation for density;
- strategy/event filtering and per-strategy runtime analytics;
- offline rejected-entry and early-exit review;
- selection of any saved session, including legacy session files where the recorded payload is sufficient.

The offline review layer never participates in live trading decisions.


## Research economics refactor

The zero-trade 2026-09-22 research run and the resulting economics/geometry refactor are documented in:

```
docs/research-economics-refactor-2026-09-22.md
```

Key current research rules:

- 0.5% structural price risk;
- 1.25% maximum planned all-in loss per trade;
- 2% aggregate open all-in risk;
- positive net target after estimated costs remains mandatory;
- old $1 minimum-net and 1.15 net R:R are shadow diagnostics;
- breakout uses re-acceptance invalidation + liquidity ladder;
- trend/rejection/density preserve their intended 1.6R target geometry instead of being truncated by the nearest liquidity level;
- confirmed entry is charged as taker, resting target as maker, stop/invalidation/partial as taker;
- strategy expectancy remains observational until enough closed trades exist.
