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

## Final diagnostic and technical blocks

```powershell
& $py -m scalp_bot.offline_trade_diagnosis $root "$out/trades-v3-entry-snapshot"
& $py -m scalp_bot.ml.plan_diagnosis "$root/dataset-v2-verified" "$root/model-v2" "$out/ml-final" --paths "$out/development-paths-mfe.jsonl"
& $py scripts/build-pr58-trade-review.py "$out/trades-v3-entry-snapshot" "$out/ml-final" docs/pr58-trade-plan
& $py scripts/audit-pr58-sampling.py $root "$out/sampling-ready-reproduced.json"
& $py scripts/benchmark-gc-probe.py $source "$root/model-v2" "$out/gc-probe" --offset 180
& $py -m pytest tests/test_pre_entry_exit_causality.py tests/test_engine_clock.py tests/test_trade_diagnosis.py tests/ml/test_plan_diagnosis.py -q
& $py -m pytest tests/test_benchmark_trace.py tests/test_pre_entry_exit_causality.py tests/test_trade_diagnosis.py tests/ml/test_plan_diagnosis.py -q
& $py scripts/test_preflight.py
node --check scalp_bot/static/app.js
node --check scalp_bot/static/replay.js
& $py -m pytest tests/test_benchmark_trace.py -q
& $py scripts/benchmark-isolated.py $source models/ml/impulse-v1-final "$out/after-window1" --offset 120 --timer-ms 1
& $py scripts/benchmark-isolated.py $source "$root/model-v2" "$out/after-window2" --offset 180 --timer-ms 1
```

trades-v3 uses the original entryLegs[0].plan.strategy_details rather than mutable closing details. Previous v1/v2 outputs remain local. The final path table defines MFE=max(0,best signed executable exit); retains best_signed_exit_bps separately; MAE=min(0,worst). Original labels are unchanged. The final model diagnostic reads only development rows and existing weights; no training command was run in this stage.

The causal sampling script independently reproduces26 ready events,23 older/missing and3 within1s. The GC probe was executed before the benchmark patch on the original harness. To reproduce that control with current tooling use a NEW output directory and `--harness-path "$out/offline_benchmark.before.py"`; its preserved SHA256 is22405dfaa7376c58fdac8f21e0e8dc81d354c774d77eb413f38423f8c433ed54. The script now records selected harness path/hash. The original gc-probe report predates that metadata addition and remains unchanged.

Regression after the maker patch:23 passed in6.09s. Combined diagnostic/stage tests:14 passed in2.23s. Full preflight:1404 passed in253.66s; output full-preflight.txt. A subsequent change only exposes three additional state-stage metrics; final trace tests pass and published-head CI validates the complete final tree. JS syntax and git diff whitespace checks pass.

Technical commits:81f5170 diagnosis;1fa6f37 pre-entry maker evidence guard;6cc28d8 benchmark archive isolation and linked stages. Benchmark source is6cc28d8. New measurement-only aggregation scripts do not run during timed series. All full runs use a production recorder, identical archived sources and model1/model2 windows as previously registered. The API timer request is scoped and restored in finally; the flag `changed` in pressure files records a successful API request, not proof that effective scheduling resolution changed. Idle controls establish the observed resolution separately.

## Final assembly and publication checks

```powershell
& $py scripts/audit-pr58-causal-controls.py "$out/trades-v3-entry-snapshot" $source "$out/causal-controls"
& $py scripts/summarize-pr58-performance.py $out "$out/performance-final"
& $py scripts/manifest-pr58-trade-review.py $out docs/pr58-trade-plan/artifact-manifest.json
& $py -m py_compile scripts/audit-pr58-causal-controls.py scripts/audit-pr58-sampling.py scripts/summarize-pr58-performance.py
```

Ready snapshots21/21; all7 receipt witnesses match the initial independent audit exactly. For each witness the original raw content hash is recomputed. The final summary preserves five old loop and three old adapter failures. All24 after runs pass the three applicable guards;22,126 forecasts,32 coalesced jobs,0 capacity drops,0 recorder drops,0 worker errors,0 stale rejections,0 proposals. Both off/shadow parity and cross-version ordinary parity pass for every fixed window/repeat. The benchmark comparison does not rerun the whole trading portfolio after the maker fix.

Exact executed diagnostic normalization was repeated into a NEW file and compared row-for-row to development-paths-mfe.jsonl (13665 equal rows):

```python
import json
from pathlib import Path
root = Path('G:/scalp-bot/data/audit-pr58-trade-plan')
rows = []
for name in ('morning-paths.jsonl', 'noon-paths.jsonl'):
    for line in (root / name).open():
        row = json.loads(line)
        row['best_signed_exit_bps'] = row['mfe_bps']
        row['mfe_bps'] = max(0, row['mfe_bps'])
        row['mae_bps'] = min(0, row['mae_bps'])
        rows.append(row)
assert rows == [json.loads(line) for line in (root / 'development-paths-mfe.jsonl').open()]
with (root / 'development-paths-reproduced.jsonl').open('x') as stream:
    for row in rows:
        stream.write(json.dumps(row, separators=(',', ':')) + '\n')
```

Artifact hashes and unchanged protected inputs are in artifact-manifest.json/final-preservation.json. These commands require a new output directory when rerun; existing guards intentionally reject overwriting evidence. Publication uses a normal push to the existing draft branch, no force or main merge. Exact final-head Linux/Windows CI receipt is saved separately after publication.
