# Scalp Bot — Paper Run v2 (10h)

This branch combines Strategy Rework v2 with a dedicated **10-hour paper-run harness**.

## Run target

The 10-hour clock starts only when **Start** is pressed in the UI.

After 10 hours the bot automatically:

1. stops accepting new entries;
2. closes any remaining PAPER positions;
3. records normal `trade_closed` events;
4. writes a `run_summary` event with elapsed time, final balance, realized PnL and trade count;
5. leaves the web server running so Replay can be inspected immediately.

Manual **Stop** does the same finalization with reason `bot_stop`.

## Replay recording for a 10-hour session

To keep the session useful without producing a needlessly huge JSONL file:

- active setup / open position: market frame every **1 second**;
- ordinary background observation: frame every **5 seconds**;
- trade/decision/risk events still record their full event snapshots.

This keeps high-resolution data around actual trading situations.

## Strategy Rework v2 included

- horizontal levels as price zones/cascades;
- stateful horizontal-zone breakout;
- Bybit public trade-flow confirmation;
- one setup = one trade, then consumed/rearm lifecycle;
- sticky active symbols;
- central opportunity arbiter;
- stale-market protection;
- net profit + net reward/risk gate;
- partial 70% at ~1R;
- 30% runner with stop moved to estimated net breakeven;
- runner target 2.5R;
- no-follow-through early loss cutting;
- strategy invalidation before hard stop;
- graceful paper-position finalization;
- paced activity scanner requests.

The numeric thresholds remain provisional and must be judged against the 10-hour Replay.

## Windows: update an existing clone

```powershell
git fetch origin
git switch paper-run-v2-10h
git pull origin paper-run-v2-10h

.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env -Force
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Then open:

- Live: http://127.0.0.1:8000/
- Replay: http://127.0.0.1:8000/replay

Press **Start** once. The UI shows the remaining time until auto-stop.

## Fresh clone

```powershell
git clone -b paper-run-v2-10h --single-branch https://github.com/branch-danya-dev/scalp-bot.git
cd scalp-bot
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

## After the run

Do not delete `data/sessions`.

The final JSONL contains the entire run, including the terminal `run_summary`. Package that session file together with branch/commit/config metadata for comparison against the first run.
