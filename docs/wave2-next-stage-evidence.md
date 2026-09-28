# Wave 2 next-stage evidence — 28 September 2026

Current continuation: [N3 native evidence](n3-native-evidence.md). The report below is
retained previous-stage evidence. W2.0 remains NOT_MET; N3.0 is incomplete.


Starting head: `01c491d7961126836e04682bed874c7f14971100`, PR61 on PR60
`1a2d0f67da0331df7d2d134bffde6f64141c7898`. Audit was committed before
implementation: [file-level plan](wave2-next-stage-plan.md). Published implementation
`b163f785ed4a85deede118e03a57fecf7633c039` has exactly the same Git tree as local
`470ee5af4ef8d01b76b235ae9bdf4de90aeaef24` used for the final historical replay:
`9113b0356bddc9784a6d515addfe7f09024193ad`. Main and PR60 were not changed.

**W2.0 is NOT_MET.** This iteration supplies diagnostic attribution, a verified
historical logical path and label-input replay, preregistered population gates,
offline shared-feed arms and an inert Demo calibration contract. It does not
close native latency, native clock-tape parity, dataset sufficiency or model
promotion. No new market capture, model fit, Demo order or execution promotion ran.

## Gate table

| Gate | Status | Evidence / remaining requirement |
|---|---|---|
| Exact-source audit and retained input inventories | MET | Plan, original primary chain receipts, raw inventory; all negative/failed attempts retained |
| W2.0 diagnostic latency attribution | MET | Native saved-stage distributions plus correlated worker/IPC/relay diagnostic; no summing percentiles |
| W2.0 six cumulative module-cost variants on complete native scheduler/clock population | NOT_TESTED | Existing 58s diagnostic is a filtered source population with a logical scheduler; it cannot meet this contract |
| Clock append and GC/codec profiling | MET | Every measured append retained; generation/pause/overlap/net block pressure measured with GC enabled |
| True allocation throughput and per-kind codec hash/JSON/gzip attribution | NOT_TESTED | Net retained blocks are not total allocation rate; new codec phase stamps have contract coverage but no acceptance measurement |
| Controlled full-load loop <=20ms / adapter <=250ms, no loss | NOT_MET | Existing fixed workload 31.3096 / 291.1001ms failed. New instrumented diagnostic also overflowed; no perf win accepted |
| Current native loop / adapter budgets | NOT_MET | Latest preserved native 24.4621 / 308.2433ms; no new native capture authorized while controlled gate fails |
| Current full historical causal setup → plan → FIRE → fill → managed close | MET | 11 preparations, one NEARUSDT close; identical ordinary events and ledger before/after this iteration |
| Persisted label-input → PaperBroker lifecycle/cost replay | MET | 2875 input operations, one complete label, exact expected/actual hash |
| Native executable-label replay and historical native output/clock equivalence | INCONCLUSIVE | Strict PR60 source binding correctly refuses current code; logical replay is explicitly not native proof |
| Bounded 900s new natural market path capture | NOT_TESTED | Preregistered duration 900s, one attempt only after controlled latency passes; no retry/extension for fills |
| Preregistered population policy and fail-closed public fitting | MET | Frozen policy and negative gate receipt; no outcome-based threshold selection |
| Multi-capture dataset promotion | NOT_MET | 3 imported capture domains, only 11 observations / 1 complete derivative label / 10 economic rejections; 0 native prepared rows in both W2 captures |
| Logistic/CatBoost, calibration, pooled/LOSO, untouched test, portfolio economic ranking | NOT_TESTED | Dataset gate blocks fitting; no trained/frozen V3 artifact |
| Immutable prepared CrossVenue snapshot and Bybit-only ablation contract | MET | Capture/epoch/receipt/exchange clocks and explicit unavailable coverage; identical-row/fold ablation tooling |
| CrossVenue incremental economic value | INCONCLUSIVE | Only complete derivative label has external coverage unavailable; no ablation fit or influence promotion |
| Development maker segmentation | MET | 4490 candidates, 808 virtually filled across two retained native captures; no positive 500ms+1000ms marginal group |
| Independent maker admission hypothesis validation | NOT_TESTED | No supported positive development segment; no hypothesis or validation capture registered |
| W2.6 / W2.7 / W2.8 execution, maker ML, applied riskScale | NOT_MET | Remain disabled; no `REJECTED_HYPOTHESIS` claimed without independent validation |
| Concrete offline shared source → two current paper arms | MET | Actual admission/PortfolioRisk/PaperBroker pass_all equivalence and deterministic veto; separate ledgers |
| Model/failure/loss/DD/finalization controls | MET | Missing/expired/wrong-source/model/intent/crash, A/B fixed loss, extra B DD, writer/gap, unresolved reconciliation, timeout |
| Actual trained-V3 paired market integration/run | NOT_TESTED | No frozen model and open W2.0; no hidden B→A fallback or market launcher |
| DemoExecutionCalibration offline protocol/contracts | MET | Frozen intent, independent event ledgers, partial/dedup/cancel/fee/precision/reconciliation; no network transport |
| Paper/Demo execution qualification | NOT_TESTED | Requires subsequent explicit owner order authorization after technical/data/model gates |

A scoped MET never implies that the broader native or economic gate is MET.
The status vocabulary is MET / NOT_MET / NOT_TESTED / INCONCLUSIVE /
REJECTED_HYPOTHESIS; the last is reserved for a tested independent hypothesis.

## Latency and recording evidence

The last native capture has 488 forecast observations. Data→adapter p99 is
308.2433ms; data→features 56.3704ms; features→prediction end 244.6470ms;
prediction end→adapter receipt 44.1362ms; adapter itself 0.0251ms. These are
separate distributions, not additive p99 components. The old trace cannot
distinguish its probe queue, parent queue, IPC and relay contributions.

New opt-in diagnostic records identify receive/parse/enqueue/callback boundaries,
queue depth, feature/probe/worker submission, worker start/prediction end,
reply receive/enqueue/parent receipt and adapter completion. Identity includes
capture, symbol, epoch, source sequence and side. Coalesced, dropped, expired,
inactive and shutdown work keeps a terminal reason. Empty stages stay unmeasured.
OS diagnostic clocks never become strategy clocks or approximate replay inputs.

One instrumented run on the unchanged historical 60s-warmup/58s failed prefix
observed 1354 replies: parent queue p99 260.9856ms; request IPC 48.3027ms;
prediction 0.8847ms; reply IPC 153.3374ms; relay enqueue 0.0040ms; relay dwell
16.3047ms; adapter 0.0661ms. Data→adapter was 659.4312ms. This supports queueing
and IPC as important diagnostic targets, not a claim that it exactly explains
the earlier native 308ms. The workloads/probe pacing differ and instrumentation
has overhead. The new per-stage codec stamps distinguish hash/JSON and gzip
for a subsequent profile; no per-kind conclusion is invented from their presence.

The measured clock producer append total was 1642.4888ms for 364261 reads
(p99 0.0129ms); other appends totalled 1993.6332ms. Codec round trips totalled
34051ms over 1836 batches. All 682 measured GC pauses overlapped active callbacks:
621 generation 0, 56 generation 1, 5 generation 2; p99 3.6205ms, max 93.1188ms.
GC remained enabled. Per-input retained-block delta p99 1542, max 16802 is
explicitly not total allocation throughput. No pooling or clock compression
rewrite was introduced on this evidence.

Writer high-water reached 33546679 of 33554432 bytes: 445634 accepted/written,
21768 rejected. The diagnostic is **INVALID for acceptance**. It preserves the
ordinary decision and portfolio hashes, but fails full input-chain equivalence
because rejected rows are real losses. Codec/drain is not exonerated. The
32MiB/32768 limits, time bounds, strategy thresholds, fees/slippage and GC policy
were not raised or relaxed. This iteration accepts no performance patch.

## Historical path and executable labels

Both exact `01c491d` and current source inspect the complete PR60 input population:
141412 market messages, 1577946 clock reads, 72895 scheduler rows and all other
scope/control receipts. Source events are executed by the same **logical**
scheduler; captured clock/scheduler observations are inspected, not consumed
as a native tape. External venues remain unavailable and do not block Bybit.

Both runs produce 1232 identical selected ordinary decision/admission/execution
events, hash `34a478c07834064bc60a628dc0e9b694dbbfefe287b887180460d163d6852e58`.
Ledger hash is `5c85f3c4660f7e9ed3b13f0b04525304f8caca072cf28ad79baa7eec060d9e9a`;
one natural historical NEARUSDT setup closes with net **−0.6697282507405 USDT**,
balance 999.3302717492595, no residual pending or position. Plan precedes FIRE;
current context/risk/economics remain authoritative.

The current derivative retains 11 first-prepared observations: 10 economic
rejections and one eligible frozen plan. Its label replays 2875 recorded input
operations through an independent label engine and PaperBroker. Exact expected
and actual output hash:
`7b4a982805adc67cabbf919d5acf6840f2214d2dcde5231d5d99ed1280b03dfd`.
Label net is **−0.9886469157565279 USDT / −0.19069932205817722 R**. This differs
from the ordinary ledger: the label owns the first-prepared unreserved frozen
plan and 250ms execution policy; ordinary FIRE rebuilds its current plan. The
label is not substituted for realized portfolio PnL. Same frozen input-path
cost/lifecycle replay is exact; native matching-source clock parity remains open.

The first current derivative failed supplemental hash validation at row 162552:
numeric flow-horizon keys changed to strings at JSON serialization. A real
codec/journal regression failed before canonicalizing context keys and passed
afterward. The failed raw capture remains untouched; the corrected offline
replay is separately named `historical-current-keyfix`. No market retry occurred.

## Dataset, external context and maker evidence

Policy `wave2-population-20260928-v1`, SHA256
`a064a63eec5478dd654ce29598b2c693f50ca5c3d52a94d842b702717dac9a4e`, is fixed
before future collection/fitting; see [population contract](wave2-dataset-promotion.md).
The assembled population hash is
`b2bd787c085eb5e9d6d683cdf4bbaccf81a3b00f697c5d54cf3fd80645ebfa06`.
Global local UTC defines membership/splits; monotonic domains remain separate.
The exact-start and corrected derivatives are not counted as independent periods.
The failed derivative is excluded explicitly for integrity, never overwritten.
Two native W2 captures add zero prepared rows but retain their manifests.

The dataset is NOT_MET: insufficient complete labels, breakout/rejection,
symbols, periods/days, LOSO support, context coverage and concentration; strict
native label replay is absent. Its successful logical label-input receipt does
not set `labelReplay=MET` in training evidence. Rejections retain null outcomes,
not zero-return trades. Missing external venues remain explicit. No production
model was fitted; numerical synthetic estimator tests are not training evidence.

Native maker development population: 4490 candidates / 808 with virtual fill;
mean net 100/500/1000ms = **−0.044643 / −0.049818 / −0.047951 USDT**. Segment
reports cover spread, queue ahead, fill delay, side and symbol; previously
unrecorded OFI/impulse/imbalance/CrossVenue/regime/volatility/depth quality remain
unavailable, not reconstructed from future data. Future candidates freeze these
covariates. Time-to-fill is an outcome, never an admission feature. No marginal
group is positive at both 500 and 1000ms. Overlapping fragments are not portfolio
PnL. There is no supported admission hypothesis and no independent shadow run.

## Offline integration and validation

`ml/paired_paper.py` feeds immutable copies of one ordered source into two current
StudyEngine/AdmissionEngine/PortfolioRisk/PaperBroker arms. `pass_all` produces
identical execution ledgers; deterministic veto affects only B as expected.
Invalid or unavailable B stops the experiment, never silently becomes A.
Finalization blocks entries, cancels pending, closes with current depth/costs,
and preserves each arm's snapshot/errors on timeout or failed reconciliation.
Offline control fills are never counted as native natural fills. A future live
runner still needs a trained/frozen artifact and exact shared-feed calibration.

`demo_paper/calibration.py` reuses Intent/Command/Order without a network venue.
It records sent/ack/partial/full/cancel/amend/stop/target events, maker/taker,
actual fees, quantities, prices and timing, and refuses foreign activity,
geometry/precision drift and unknown exposure. Amend acknowledgements are
diagnostic events only; production amend/reconciliation integration is still
part of future qualification. Empty ledgers cannot complete the contract.
See [Demo protocol](demo-execution-calibration.md); no credentials/account access
or order calls occurred.

Confirmed red→green receipts cover population sufficiency, entire-episode embargo
purge, failed finalization snapshots, research source sequence, Demo geometry/empty
reconciliation and context JSON/hash integrity. The first broad targeted run
also caught an ordinary-runtime ML import violation; the harness was moved into
`ml/` without weakening the architecture guard.

Implementation GitHub [run 36410907199](https://github.com/branch-danya-dev/scalp-bot/actions/runs/36410907199)
is green on exact `b163f785ed4a85deede118e03a57fecf7633c039`: Linux/Python 3.12
**1652 passed, 6 skipped**, Windows optional ML/spawn/codec/CrossVenue and new
contracts **521 passed**, both JS syntax checks passed. Local full Windows
preflight on final source: **1666 passed in 229.07s**; its receipt is included
in the evidence bundle. Local WSL was unavailable, so Linux ran on the actual CI Ubuntu
runner. The final docs/evidence head requires its own exact-head CI verification.

## Provenance and remaining work

Evidence bundle: [next-stage/](wave2-evidence/next-stage/). It includes full
source/config/runtime receipts, dataset membership/manifests, model hashes,
red/green logs, compact stage distributions, historical comparison, studies and
raw inventories. Local source-on-disk SHA256:
`fb6b852a9b8339827f5d6ce8e33752c21705407faa065f4e01474680c2ba79ba`.
Primary compressed-file SHA256 is
`28c60234642cf52fbf01f0a3ae773b819f8949e84b3884cee6a573597a843a2d`;
decoded JSONL integrity SHA256 is
`834ea1f7742311dc19ab9278d17ea4fcd6cafeee5ecf83854ec4d75009a73054`.
Their hash bases differ deliberately. V3 dataset/split/model/calibration promotion
is absent; missing artifact hashes remain null, not placeholders.

Next blocking engineering work is still the complete six-variant native
scheduler/clock-preserving module-cost harness, end-to-end allocation/codec
attribution and a semantically equivalent latency fix. Only after controlled
budgets pass may one preselected 900s public-paper capture run; no automatic
retry or extension. That capture must prove the full natural path, native labels,
zero critical loss/backpressure and both valid chains. Then collect independent
data under the frozen policy before fit/LOSO/untouched test and portfolio ranking.
No 30–60m paired market, 8h/12h, maker execution/ML, applied riskScale, Demo or
mainnet action is authorized by green tests or this report.
