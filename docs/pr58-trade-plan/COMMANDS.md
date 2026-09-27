# Execution log — trade plan and latency task

Workspace: C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot.
Python: ./.venv/Scripts/python.exe (3.13.15), imported scalp_bot from this worktree.
Initial local/remote head1d7d9aba6c378cba70403d10234b26d9f7df367e, draft PR58, clean worktree. Original main checkout stays3403b03 with prior untracked data/pr58-completion; no tracked baseline edits. Protocol frozen in commit62c2853 before new grouped outcome calculations.

The commands below were executed with literal paths; aliases only shorten this log. Outputs use new directories; prior exploratory versions remain preserved. No market runner, model training, new historical download, orders or main merge was executed.

```powershell
$py = './.venv/Scripts/python.exe'
$root = 'G:/scalp-bot/data/pr58-completion'
$out = 'G:/scalp-bot/data/audit-pr58-trade-plan'
$source = 'G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite'
& $py -m scalp_bot.offline_trade_diagnosis $root "$out/trades-v1"
& $py -m scalp_bot.ml.plan_diagnosis "$root/dataset-v2-verified" "$root/model-v2" "$out/ml-v1"
& $py -m scalp_bot.ml.plan_paths "$root/morning-verified/inputs.sqlite" "$root/dataset-v2-verified/dataset.jsonl" capture-20260926T093515Z "$out/morning-paths.jsonl"
& $py -m scalp_bot.ml.plan_paths "$root/noon-verified/inputs.sqlite" "$root/dataset-v2-verified/dataset.jsonl" capture-20260926T121926Z "$out/noon-paths.jsonl"
# Concatenated the two immutable path files into new development-paths.jsonl.
& $py -m scalp_bot.ml.plan_diagnosis "$root/dataset-v2-verified" "$root/model-v2" "$out/ml-v2-path-audit" --paths "$out/development-paths.jsonl"
& $py -m scalp_bot.offline_trade_diagnosis $root "$out/trades-v2"
& $py -m pytest tests/test_pre_entry_exit_causality.py -q
& $py -m pytest tests/test_trade_diagnosis.py tests/ml/test_plan_diagnosis.py -q
& $py scripts/benchmark-isolated.py $source models/ml/impulse-v1-final "$out/before-window1" --offset 120
& $py scripts/benchmark-isolated.py $source "$root/model-v2" "$out/before-window2" --offset 180
```

trades-v1 initially attached same-symbol/side management events too broadly; trades-v2 filters by setupId and actual open/close monotonic interval. This is a diagnostic association correction, not changed historical executions. Both outputs preserved. All cash identities and delta reconciliation pass. Path audit6482+7183 rows: all covered, original entry/exit and first barrier match. ML rows13665; test2847/purged278 excluded from new analysis.

Pre-entry execution regression before production change:1 failed/1 passed, exact failure and traceback in pre-entry-regression-before.txt. This is the intended reproduction of a later maker exit filled by an earlier received batch; protective-stop control passes. Diagnostic tests:9 passed, diagnostics-tests.txt.

Historical raw receipt witnesses are saved in pre-entry-historical-evidence.json, with original source sequence and opened/received/processed monotonic timestamps. sampling-ready-audit.json compares all26 causal first-ready events in the additional development captures to preceding retained10s-grid observations; it does not select by future returns. starting-preservation.json records dataset/model/policy/control and source-code checksums before technical patches.

Initial performance measurement uses the unchanged offline_benchmark.py copied to offline_benchmark.before.py. Pressure sampler reads Windows PDH counters once per second in a measurement thread, buffers in memory and writes after measurement. No own training/replay/tests/indexing/download jobs run concurrently with benchmark. environment-before.json saves hardware/OS and process inventory; foreign processes are never stopped. This is isolation from own compute jobs, not proof of an idle machine.

PR59 immutable JSON and protocol were read through GitHub at c67b46cbbc982ec404df728ef874430a77a056ee because that commit was unavailable locally. No merge or edit of those files. launch_authorized=false, selected_mode=null, duration28800 and goalsG1-G4 remain unchanged.
