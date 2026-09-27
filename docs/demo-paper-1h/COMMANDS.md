# Executed offline verification

Worktree `C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot`; separate `.venv`, Python 3.13.15. No Demo credentials supplied, authenticated probe, public market connection, create order or measured hour was run. All below are completed commands except the explicitly identified final CI receipt, supplied after GitHub finishes.

```powershell
.venv/Scripts/python.exe -m scalp_bot.demo_paper preflight --model-dir G:/scalp-bot/data/pr58-completion/model-v2 --output G:/scalp-bot/data/audit-demo-paper-1h-local-check
.venv/Scripts/python.exe scripts/check-demo-paper-offline.py --source G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite --model G:/scalp-bot/data/pr58-completion/model-v2 --dataset G:/scalp-bot/data/pr58-completion/dataset-v2-verified --output G:/scalp-bot/data/audit-demo-paper-preparation/offline-v1
.venv/Scripts/python.exe scripts/check-demo-paper-offline.py --worker-only --source G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite --model G:/scalp-bot/data/pr58-completion/model-v2 --dataset G:/scalp-bot/data/pr58-completion/dataset-v2-verified --output G:/scalp-bot/data/audit-demo-paper-preparation/worker-isolated-v2
.venv/Scripts/python.exe -m pytest -q tests/demo_paper tests/ml/test_foundation.py tests/test_pre_entry_exit_causality.py tests/test_transport_recovery.py
.venv/Scripts/python.exe -m pytest -q tests/demo_paper tests/test_paper.py
.venv/Scripts/python.exe scripts/test_preflight.py
```

The first full suite failed only the prior blanket non-ML-directory import assertion (1432 passed, 1 failed). The newly opt-in `demo_paper` namespace is now explicitly permitted, while a subprocess verifies ordinary `scalp_bot.engine` imports neither ML nor Demo research. Existing ordinary import checks remain. A synthetic funding test initially placed its next boundary inside the declared 2-second matching tolerance; its next boundary was corrected to a separate interval, without changing the reconciliation rule. Failed logs remain local.

Earlier targeted groups: 120 passed in 5.77 seconds (lifecycle, ML foundation, maker receipt and transport); 83 passed in 3.84 seconds (Demo plus original paper). Historical replay retains every selected market message; captured controls are hashed/counted but cannot authorize a network run. It checks input integration rather than Demo PnL. Worker inference uses original validation feature values with explicit original/local clock refs retained, never trains or tunes.

Final full-suite/CI result: see [VALIDATION.md](VALIDATION.md). Local evidence stays in the above `audit-demo-paper-preparation` directory, and `evidence-manifest.json` records file hashes. Old reports/models/data are not overwritten.


Final source audit additionally runs `tests/demo_paper/test_reporting_boundaries.py` and `tests/demo_paper/test_ml_audit_contracts.py`. Their before/after failures are retained; the first ML audit fixture omitted the session symbol and was corrected before reproducing the actual missing funnel records.

The final bounded replay/inference invocation repeats the first command with output `G:/scalp-bot/data/audit-demo-paper-preparation/offline-final-v3`. The source event checksum, 36,121 selected events, 4,757 evaluations, 60 forecasts and zero selections agree. Its compact report is retained in `historical-final.json`. These are functional checks, not a new isolated latency benchmark.

The Windows launcher itself was executed with `-Action Preflight -Output G:/scalp-bot/data/audit-demo-paper-1h-launcher-check`; its local receipt contains `connected=false`, `orders_sent=0`, `launch_authorized=false`. ConnectedPreflight and Start were not executed.
