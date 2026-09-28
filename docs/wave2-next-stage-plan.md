# Wave 2 gate closure: audited implementation plan

Frozen starting source: `01c491d7961126836e04682bed874c7f14971100`, draft PR61,
stacked on PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`.
GitHub run `36404551255` was independently checked: success. This is a source
and contract audit, not evidence of profitability or current native latency.

## File-level audit before implementation

| Files / boundary | Observed state and required work |
|---|---|
| `bybit.py:49,761,876`, `market_runtime.py`, `latency_observability.py:309` | Receive/parse/queue/book/strategy timing exists. Add correlated callback completion and queue distributions without changing recorded decision clocks. |
| `scripts/smoke-trading-model.py:ShadowProbe`, `ml/worker.py:17,50` | V2 probe serializes long/short through a 32-entry probe queue, a coalescing worker map, one IPC request and a one-entry reply relay. Prediction duration exists; parent queue/IPC/relay dwell are not independently measured. Attribute these before any scheduling change. |
| `offline_benchmark.py`, `offline_benchmark_trace.py`, `scripts/profile-capture-model.py` | Existing 58s workload filters input kinds and uses a logical scheduler. It is a failed diagnostic prefix, not a complete native event/scheduler/clock replay. Do not promote its module comparison as strict replay evidence. |
| `capture.py:70`, `capture_codec.py:49`, `input_journal.py:299,346` | Scalar deferred clock rows and bounded codec already exist. Profile all clock append/encode/hash/drain costs and GC pauses. No representation change without measured benefit and complete observation/hash/order equivalence. |
| `offline_bootstrap.py:restore_cold_engine`, `offline_scheduler.py:apply`, `capture_replay.py` | Strict replay binds capture source and runtime. PR60 -> current is an explicit cross-source causal experiment; a source mismatch must remain visible. Never monkeypatch attestation or silently replace the native clock tape with logical time. |
| `ml/wave2.py:prepared,book,trade,context`, `engine.py` admission hooks | First-prepared features, detached structural plan, segment and immutable external snapshot exist. Historical Bybit-only replay needs explicit external unavailability and all label-driving callback inputs. |
| `ml/prepared_labels.py:PreparedPath`, `paper.py`, `tests/ml/test_prepared_labels.py` | Reuses PaperBroker, delayed entry, costs and censoring. Verify full execution parity, normalization/depth provenance, batch fencing, partial/runner/context exit and exact plan/source identity. Native label replay remains unproven. |
| `ml/prepared_learning.py:verify_dataset_evidence,evaluate,train_frozen`, `ml/prepared_dataset.py` | Integrity hashes and UTC splits exist; population coverage/concentration promotion gate is absent. Add preregistered immutable policy, multi-capture join and fail-closed fit gate retaining rejected/censored membership. |
| `ml/prepared_learning.py:matrix,report`, `ml/cross_venue_study.py` | Cross-venue features always included; no paired Bybit-only ablation. Independent label means are correctly not portfolio PnL. Add identical-membership ablation contract; training/economic promotion remain blocked by dataset and portfolio evidence. |
| `maker_shadow.py`, `ml/wave2.py:book` | Strict queue/volume shadow exists, latest costs negative. Segment covariates are incompletely attached. Report unavailable historical covariates honestly; no hindsight feature reconstruction. Freeze one hypothesis only if development evidence supports it. |
| `experiment_controller.py`, `ml/prepared_adapter.py`, `engine.py:_arbitrate_once` | Guard/controller contract exists, concrete paper arms absent. Integrate shared recorded feed with actual engine admission and independent brokers. Synthetic pass-all/veto plus model/finalization failures; no market launcher. |
| `demo_paper/contracts.py:Intent,Command,Order`, `demo_paper/venues.py` | Frozen intent and independent ledgers already exist. Add inert execution calibration/report contract and offline fixtures. No network clients, account access, credentials or order calls in the new harness. |
| `.github/workflows/strategy-rework-checks.yml`, `scripts/test_preflight.py` | Full Linux suite, Windows ML/spawn/codec suites and two JS syntax checks. Extend targeted coverage, run full Windows and Linux on native filesystem, then verify exact published PR head. |

## Ordered implementation and evidence

1. Preserve immutable source, capture inventories and this plan before implementation.
   Historical PR60 natural-fill capture and both failed W2 native captures remain untouched.
2. Record confirmed defects as failing regressions, then fix and retain passing receipts.
   Add correlated stage/queue/GC/codec telemetry. Run profiling separately from acceptance
   timings; retain failed/incomplete attempts. All perf patches need decisions, portfolio,
   input and complete clock/event order equivalence.
3. Attempt current-source causal/native replay of the historical NEARUSDT path. Preserve
   strict source/config/runtime/tape checks and report PASS/FAIL/INCONCLUSIVE precisely.
   Prove label/PaperBroker parity independently of normalized cross-venue replay.
4. Enforce frozen dataset policy before all production fit/test entry points. Build the
   complete multi-capture observation population using global UTC, explicit missing venue
   coverage and censored gaps. No labels or outcome zeros invented for rejected intents.
5. Add concrete offline shared-feed paper arms and inert Demo execution calibration.
   Exercise control parity, one deterministic veto and all specified failure contracts.
6. Analyze development maker shadow and cross-venue coverage without promoting mechanics.
   Fit models only after dataset gate; market paired run only after frozen V3 and all gates.
7. Full validation, exact hashes and raw inventories; update HANDOFF, roadmap and separate
   next-stage evidence table with MET / NOT_MET / NOT_TESTED / INCONCLUSIVE /
   REJECTED_HYPOTHESIS. Publish only to PR61 and verify exact-head CI.

## Runtime boundaries

No strategy/risk/economics/fee/slippage changes; no queue/memory/time budget increases.
GC remains enabled. No maker execution/ML, applied segment riskScale, paired market run,
8h/12h, Demo orders or mainnet. A single preselected 600–900s public-paper capture is
conditional on passing controlled full-load latency; it is not authorized while that
gate is open. No retry/extension for natural fills. An independent maker capture is
conditional on a preregistered supported hypothesis and technical gates.
