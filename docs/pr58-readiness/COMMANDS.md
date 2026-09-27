# Executed offline commands

Working directory: `C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot`; Python is that directory's `.venv/Scripts/python.exe`. Each output directory below was new. No live runner was launched. The original raw/index, v1 dataset and weights were read-only.

```powershell
$py = './.venv/Scripts/python.exe'
$root = 'G:/scalp-bot/data/pr58-completion'
$source = 'G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite'
& $py -m scalp_bot.ml.diagnostics data/ml/impulse-v1-final models/ml/impulse-v1-final docs/pr58-readiness/abstention_diagnostics.json
& $py -m scalp_bot.offline_study $source "$root/A" --variants A
& $py -m scalp_bot.offline_study $source "$root/B" --variants B
& $py -m scalp_bot.offline_study $source "$root/C" --variants C
& $py -m scalp_bot.offline_market_index $source $root "$root/market-index"
& $py -m scalp_bot.offline_comparison $root "$root/market-index/quotes.sqlite" docs/run-reviews/two-hour-20260926/scenario_funnel.csv docs/pr58-readiness
& $py -m scalp_bot.offline_index G:/scalp-bot/data/paper-captures/1h-20260926-123243-230-fcf4035f/session-20260926T093515Z.inputs.jsonl.gz "$root/morning-verified"
& $py -m scalp_bot.offline_index G:/scalp-bot/data/paper-captures/24h-20260926-151653-835-9dde484e/session-20260926T121926Z.inputs.jsonl.gz "$root/noon-verified"
& $py -m scalp_bot.offline_study "$root/morning-verified/inputs.sqlite" "$root/morning-context" --variants B --capture-id capture-20260926T093515Z
& $py -m scalp_bot.offline_study "$root/noon-verified/inputs.sqlite" "$root/noon-context" --variants B --capture-id capture-20260926T121926Z
& $py -m scalp_bot.ml.combine_periods "$root/morning-context/dataset" "$root/noon-context/dataset" docs/ml/experiments/pr58-documented-periods.json "$root/dataset-v2"
& $py -m scalp_bot.ml train --dataset "$root/dataset-v2" --output "$root/model-v2"
& $py -m scalp_bot.ml evaluate --dataset "$root/dataset-v2" --model "$root/model-v2"
& $py -m scalp_bot.ml predict --dataset "$root/dataset-v2" --model "$root/model-v2" --row 0
& $py -m scalp_bot.ml.context_audit "$root/dataset-v2" docs/pr58-readiness/context-coverage-v2.json
& $py -m scalp_bot.ml.diagnostics "$root/dataset-v2" "$root/model-v2" docs/pr58-readiness/abstention-v2.json
& $py -m scalp_bot.ml shadow --dataset "$root/dataset-v2" --model "$root/model-v2" --output "$root/shadow-v2-verified" --limit 1200
& $py scripts/check-ml-shadow-episodes.py "$root/model-v2" docs/pr58-readiness/shadow-episodes-v2.json
& $py -m scalp_bot.offline_benchmark $source models/ml/impulse-v1-final "$root/latency" --seconds 60 --repeats 3
& $py -m scalp_bot.offline_benchmark $source "$root/model-v2" "$root/latency-v2-window2" --seconds 60 --repeats 3 --offset 180
& $py scripts/test_preflight.py
```

The block normalizes repeated literal paths into PowerShell variables; the executed individual tool calls used those same literal paths. Reproduction requires new output directories (existing artifacts are deliberately rejected). Training took 2.212s, returned zero, saved real CatBoost/logistic weights and passed exact reload. Evaluation/predict/shadow results are adjacent JSON files.

Initial exploratory prefixes and a StudyRecorder-only benchmark are retained locally but are not the final production-recorder comparison. The first full test attempt (3 failures) is preserved: explicit offline tools conflicted with an overly broad import guard; the transport test expected the pre-diagnostics callback shape; one replay provenance check correctly detected source edits during that run. Runtime import isolation is now checked in a clean subprocess; new diagnostics are asserted explicitly. Final full tests run with source files frozen.

After training, combine_periods was also executed to the new `dataset-v2-verified` directory to report exclusions from both sources (not only the first manifest). Dataset SHA is identical; weights and numeric evaluation are unchanged. No baseline/model was overwritten.
