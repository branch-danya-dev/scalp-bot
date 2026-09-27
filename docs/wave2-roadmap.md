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

## Evidence status — 28 September 2026

The audited implementation is on `codex/wave2-shadow`; the detailed observation
contract is [wave2-research-protocol.md](wave2-research-protocol.md). Exact source,
configuration, runtime and raw file hashes are in
[wave2-evidence/summary.json](wave2-evidence/summary.json).

| Stage | Implemented / observed | Remaining gate |
|---|---|---|
| W2.0 | Exact PR60 300s paper capture preserved | **NOT MET**: 0 fills; input writer 32MiB bound exceeded, no footer; loop p99 64.0391ms and data→adapter 894.1426ms |
| W2.1 | Existing evidence store extended; five-state SHADOW lifecycle; replay/dormant recovery tests | No independent complete shadow labels to fit real transitions; no applied risk change |
| W2.2 | Public Binance/OKX adapters, explicit clocks/units/epochs, supplemental hash-chain and normalized replay | No live trading influence; sustained data/clock validation still needed |
| W2.3 | Offline purged strategy/alignment/symbol/regime study command | **INCONCLUSIVE**: W2 market capture had no prepared intents |
| W2.4 | Virtual queue/partial fill/cancel/markout engine; one capture produced 377 candidates, 64 with any fill, 133 fill fragments | Short development markouts negative on average; independent positive evidence absent |
| W2.5 | Frozen structural label engine using PaperBroker, global wall provenance, Logistic/Ridge + CatBoost, calibration/LOSO, one-time test receipts, identity-bound adapter | No trained V3, verified native label replay dataset, economic promotion or untouched test; fitting rejects unverified dataset manifests |
| W2.6–8 | Intentionally not enabled/implemented before required evidence | Maker playbook → maker ML → applied registry risk remain gated |
| W2.9 | Tested external paper controller: common input contract, independent ledgers, fixed loss/DD/hash/timeout/finalization guards | Concrete live paired arm/feed/inference integration and 30–60m paired run not completed; 8h forbidden |

Unified integration capture at source `56c2583c6eef6a4d872f3eb21a6299c9ee511ae4`:
60 seconds, 6 Bybit symbols; 327573 primary input rows and 34849 supplemental rows,
both hash chains complete, no recording drops/errors. Loop p99 **12.9586ms**,
data→adapter **224.7352ms**, capture detach **0.2735ms**. No first-prepared setup or
ordinary fill occurred. These values do not close natural-fill or ML-incremental
latency gates and do not demonstrate a performance improvement against the much
busier baseline capture. No runtime retry or parameter relaxation was used.

Public-only connectivity probe: 716 normalized Binance/OKX events for BTC/ETH,
both venues available; no credentials/private calls. It is a transport check,
not another trading sample. Cross-venue normalized-event replay passed, but the
zero-setup capture cannot test a real prepared-context join; synthetic integration
fixtures cover that join.

Maker virtual markouts (per fill fragment; **not portfolio returns**): mean net
100/500/1000ms = -0.053827/-0.061764/-0.057101 USDT. The 1000ms event stream has 131
complete observations and one window-gap censor; another lot was censored during
shutdown in its final candidate outcome. Do not select only complete winners or
sum these horizons. Detailed counts are in [maker-summary.json](wave2-evidence/maker-summary.json).

Confirmed regressions have red/green receipts: smoke acceptance ignored fatal
input-writer loss; same-batch maker exit leakage in new labels; double-counted
calibration boundary; incomplete dataset evidence gate; one failing arm stop
callback skipped disabling the other. The ordinary-runtime ML import boundary
also failed the full suite and was repaired without weakening its test.

## Next work, in order

1. Diagnose and bound the PR60 capture writer/loop failure under comparable load;
   do not raise queue budgets. Preserve the incomplete baseline and all receipts.
2. Obtain one bounded causal prepared→economic→FIRE→natural fill→exit observation
   when the market offers it, under unchanged economics. No automatic repeat loop.
3. Validate executable label replay against primary raw, add exact per-capture
   integrity/replay/source/config/runtime evidence, then accumulate independent
   symbol/regime/capture periods. Current observations cannot train or promote V3.
4. Run cross-venue/maker studies on those complete populations. Keep maker execution
   disabled unless independent net after costs is positive.
5. Complete the concrete shared-feed paired adapter with the admitted frozen V3
   inference artifact. Only then a 30–60m technical run; 8h requires all gates.

Raw and failed attempts remain under this chat's `work/`: `baseline-300s`,
`wave2-integration-60s`, `public-cross-venue-probe`, `wave2-replay-60s` and red/green
test logs. The 60s capture source remains frozen separately from subsequent
offline/control fixes. No mainnet/Demo order, 8h run, merge, fee/risk relaxation,
V2 weight or .55 threshold change occurred.
