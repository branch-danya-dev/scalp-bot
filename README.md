# Scalp Bot

Paper-first prototype of an autonomous Bybit scalping workflow.

The first version is deliberately small: it watches a few liquid USDT perpetuals, trades only with the higher-timeframe structure, rejects setups that do not cover transaction costs plus a minimum net profit, and records the bot's decisions together with market snapshots for review.

## MVP

- Bybit mainnet public market data; no API key is required in paper mode.
- Scanner filters USDT perpetuals by 24h turnover (150M USD by default) and watches the top three.
- 15m market structure is the global direction filter: no counter-trend trades.
- Strategies:
  - trend structure pullback / trend-line bounce;
  - horizontal level bounce;
  - order-book density bounce.
- One paper position at a time.
- Position size comes from stop distance and maximum allowed capital risk.
- Hard cost gate includes taker fees, spread and modeled slippage.
- Minimum expected net profit is 1 USD by default.
- Entries, exits and rejected trades include market snapshots in a JSONL session log.
- Local macOS-like control UI shows working symbols, live candles, order book, strategies, decisions, balance and PnL.

## Run

Requires Python 3.12+.

1. Create a virtual environment: python -m venv .venv
2. Activate it.
3. Install: pip install -e ".[dev]"
4. Copy .env.example to .env
5. Start: uvicorn scalp_bot.app:app --reload --host 127.0.0.1 --port 8000
6. Open http://127.0.0.1:8000

The application starts in observation mode. Market data is live, but paper positions are not opened until the Start button is pressed.

## Important defaults

SCALP_START_BALANCE=1000
SCALP_MIN_NET_PROFIT_USD=1
SCALP_RISK_FRACTION=0.005
SCALP_MAX_LEVERAGE=1.0
SCALP_TAKER_FEE_RATE=0.00055
SCALP_SLIPPAGE_BPS=1.0

MAX_LEVERAGE=1.0 is intentionally conservative for the first verification run. The fee defaults model Bybit VIP 0 perpetual/futures taker fees; verify the actual fee tier on the account before any future live-trading phase.

## Session review

Every launch creates a data/sessions/session-<UTC timestamp>.jsonl file. It records scanner changes, strategy reasoning, risk rejection, entries and exits. Critical events contain the candle and order-book state so we can later build a full replay of the bot's workday.

## Current limits

This is an MVP, not a live trading system. It intentionally has no authenticated order execution and no AI layer yet. Historical replay UI, persistent database, authenticated Bybit execution and model assistance belong to later phases after the paper workflow produces useful review data.
