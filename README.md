# Scalp Bot — Current 10h Paper Run

This run branch is the current integrated project state. It combines the 10-hour research harness with every strategy rework completed before this run.

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
- paper positions finalize on Stop, shutdown, or 10h deadline;
- one position may use at most 25% of portfolio exposure by default, so a tight scalp stop cannot monopolize all capital.

## Research-run rules

There is deliberately **no cumulative session-loss kill switch**:

```
SCALP_ENFORCE_SESSION_LOSS_LIMIT=false
```

Per-trade and simultaneous portfolio risk controls remain enabled.

## 10-hour harness

The timer starts after pressing Start.

At the deadline:

- no new positions;
- remaining paper positions are closed;
- trade_closed events are recorded;
- run_summary is written;
- server remains available for Replay.

Replay sampling:
- 1 second while a setup/position is engaged;
- 5 seconds during idle observation;
- important decisions/trades carry event snapshots.

## Windows update

```powershell
git fetch origin
git switch paper-run-v2-10h
git pull origin paper-run-v2-10h

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
