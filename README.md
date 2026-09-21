# Scalp Bot

Paper-first prototype of an autonomous Bybit scalper. The goal of the current stage is not to maximize win rate; it is to reproduce a disciplined trader workflow that can be reviewed visually after a live market session.

## Current workflow

1. Filter Bybit USDT perpetuals by 24h turnover (150M USD by default).
2. From that liquid universe, rank coins by absolute price movement over the last 5 minutes, with recent turnover as a tiebreaker.
3. Promote the top symbols to **active symbol sessions**. Each active coin has isolated candles, order book, strategy decisions and replay frames.
4. Trade only in the direction of the higher-timeframe structure.
5. Wait for one of the enabled setups:
   - trend structure / trend-line bounce;
   - horizontal level bounce;
   - order-book density bounce.
6. Before entry, reject the setup if:
   - the setup has already moved too far from the intended entry (entry drift / no chasing);
   - the stop has already been invalidated;
   - the portfolio has no exposure/risk budget left;
   - expected gross profit does not cover fees + spread + slippage + minimum required net profit.
7. A negative unrealized PnL is **not** an exit signal by itself. The paper position stays open until its pre-defined stop/invalidation or target is hit.
8. Record MAE/MFE so we can measure how far successful and failed trades moved against/for us before closing.

## Parallel positions and resource isolation

The bot may monitor and trade several active coins asynchronously, but there is at most one open position per symbol. Positions share one portfolio balance and one total exposure/risk budget, so separate symbol workers cannot reuse the same capital independently.

## Visual session replay

Every run produces data/sessions/session-<UTC timestamp>.jsonl.

For each active symbol the recorder stores:

- bootstrap candle history when the coin becomes active;
- roughly one market frame per second;
- current 1m candle;
- top of the order book;
- trend state;
- open-position state;
- strategy decisions and reasons;
- watched levels / trend-line overlays;
- risk rejections;
- entry, stop, target and execution plan;
- trade close result, fees, MAE and MFE.

Open /replay to select a session and symbol, scrub the timeline, inspect the chart and order book at that exact moment, and jump directly to bot events.

## Run

Requires Python 3.12+.

1. python -m venv .venv
2. Activate the environment.
3. pip install -e ".[dev]"
4. Copy .env.example to .env.
5. uvicorn scalp_bot.app:app --reload --host 127.0.0.1 --port 8000
6. Live UI: http://127.0.0.1:8000
7. Replay UI: http://127.0.0.1:8000/replay

The app starts in observation mode. Public Bybit mainnet data is live, but all orders are simulated locally.

## Important defaults

SCALP_START_BALANCE=1000
SCALP_MIN_TURNOVER_USD=150000000
SCALP_LIQUID_UNIVERSE_SIZE=30
SCALP_WORKING_SYMBOLS=4
SCALP_ACTIVITY_WINDOW_MINUTES=5
SCALP_MIN_NET_PROFIT_USD=1
SCALP_RISK_FRACTION=0.005
SCALP_MAX_TOTAL_RISK_FRACTION=0.02
SCALP_MAX_LEVERAGE=1.0
SCALP_MAX_OPEN_POSITIONS=4
SCALP_MAX_DAILY_LOSS_FRACTION=0.03
SCALP_MAX_ENTRY_DRIFT_BPS=8
SCALP_TAKER_FEE_RATE=0.00055
SCALP_SLIPPAGE_BPS=1.0
SCALP_REPLAY_FRAME_SECONDS=1

Fee settings are configuration values, not assumptions that should be hard-coded forever. Verify the real Bybit account fee tier before any future live-trading phase.

## Verification

Current local test suite: 8 passed.

The tests cover the cost gate, no-chasing rule, an initially losing position that remains valid until its stop, per-symbol position isolation with a shared portfolio exposure budget, replay serialization and basic trend classification.

## Not implemented yet

- authenticated live Bybit execution;
- automatic stop movement / trailing logic;
- advanced strategy invalidation before the hard stop;
- database-backed long-term dataset;
- AI/ML layer.

Those are intentionally deferred until a short live-market paper run shows that the scanner, strategy reasoning, risk logic and replay are behaving coherently.
