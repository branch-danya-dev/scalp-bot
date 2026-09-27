# Wave 2: audited plan and evidence

Baseline: draft PR60, `1a2d0f67da0331df7d2d134bffde6f64141c7898`.
Work branch: `codex/wave2-shadow`. Main and PR60 remain unchanged.
PR60 final-head GitHub workflow 36356147417 succeeded. No review threads were
present at audit time. Green CI does not close the natural-fill gate.

## Audit before implementation

* `scenario_runtime.py: _scenario_decision/_observe_prepared_intent` captures the
  first causal prepared intent, before economic admission. The collector freezes
  features, but does not yet freeze an executable plan or wall-time provenance.
* `admission.py: _arbitrate_once/_dispatch_admitted` rechecks context, rebuilds
  economics against sequential reservations, checks portfolio availability, then
  emits FIRE. Extend observation only; keep this decision path authoritative.
* `setup_segments.py` owns the eight dimensions; `expectancy.py` deduplicates
  valid closed-position evidence. Extend that store with uncertainty/recent/DD
  evidence; a separate lifecycle owns states, never a second key or ledger.
* `market_runtime.py` applies publicTrade one tick at a time before invalidation.
  Preserve this ordering for maker research; no batch lookahead or touch fills.
* `paper.py` owns fees, fill volume, partial/runner and management semantics.
  Research labels must reuse PaperBroker rather than approximate quote returns.
* `input_journal.py`/`capture.py` already detach and hash in bounded FIFO writers.
  Supplemental cross-venue/research capture needs its own ordered, bounded,
  verifiable stream linked to the primary capture and source sequence. No changes
  to existing replay-input schema or clock tape just for research telemetry.
* `ml/prepared_dataset.py` provides intent identity, PreparedForecast and purged
  walk-forward. Keep V2 schema/weights/.55 unchanged; V3 dataset is separate.
* `docs/trading-model-next-8h-protocol.md` is inert. Model/data/adapter/paired-run
  evidence is missing. A controller alone cannot authorize an 8-hour experiment.

## Implementation sequence and verification

1. W2.0: one 300-second baseline public capture, unchanged risk/economics, frozen
   V2 load probe; inspect natural entry/exit, p99 loop 20ms, adapter 250ms, drops,
   input hash/scope integrity. No fill means INCONCLUSIVE, no forced retest.
2. W2.1: extend SegmentExpectancyBook evidence; add shadow SegmentRegistry with
   minimum samples, recent/long bounds, drawdown, hysteresis/residence. Tests:
   deduplication, invalid/censored evidence, replay, dormant recovery, no mutation
   of admission/risk. Shadow-label population stays distinct from positions.
3. W2.2: add public-only cross_venue runtime/adapters, explicit capture/epoch/
   exchange/receipt/processing clocks, read-time freshness and causal windows.
   Integrate supplemental capture at first-prepared and Bybit market boundaries.
   Tests: skew, stale/missing venues, out-of-order, reconnect/gaps, async arrival.
4. W2.3: offline cross-venue study over identical prepared identities; strategy,
   symbol/regime/capture/fold strata, economic and path metrics; no overlapping
   label portfolio sum. Unavailable data cannot support promotion.
5. W2.4: separate MakerShadowEngine, publicTrade/side/through/queue/volume evidence,
   virtual partial fills, cancellation/epoch/depth censoring and preregistered
   post-fill markouts. Ordinary broker and PortfolioRisk have no dependency on it.
6. W2.5: frozen executable plan and wall-time provenance, PaperBroker-based labels,
   gaps censored, separate calibration/test, pooled/LOSO logistic + existing
   CatBoost baseline pipeline, economic ranking and Brier/coverage/concentration.
   Add identity/source/hash/expiry adapter contract; no live promotion by default.
7. W2.6/W2.7/W2.8: BLOCKED until independent positive shadow/deterministic/ML
   evidence respectively. Do not create or enable maker execution, maker ML
   decisions or registry risk scaling before their required evidence.
8. W2.9: fail-closed external paper experiment controller, fixed 30 USDT loss/DD
   each arm, zero extra B drawdown, hashes/gates before Start, bounded finalization,
   no fallback/restart/extension. Paired market run needs an admitted model and
   verified independent ledger integration; no long run from synthetic tests.

Every confirmed defect gets a failing regression receipt before correction.
New functionality gets contract tests. Full preflight and CI follow targeted
tests. Preserve failed attempts/raw and report missing gates explicitly.

## Unified capture design

One ordinary public Bybit paper capture plus optional research observer records
segment state/evidence, Binance/OKX context, maker candidates and causal prepared
V3 rows. External venues are telemetry only. Missing external data invalidates
research coverage, never Bybit safety. Research resource failure is recorded and
disables research collection; it cannot silently claim complete evidence.

## Evidence status

Implementation and new market evidence pending. No profitability claim, trained
V3 model, maker strategy, adaptive risk, or 8h authorization exists at this stage.
