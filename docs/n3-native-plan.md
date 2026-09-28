# N3 native qualification: audit and implementation plan

Starting PR61: `179af6dce3b890ffd726a8528808d3488d664605`, stacked on
PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main/PR60 are immutable.
This plan precedes implementation. Existing next-stage evidence remains authoritative.

## File-level audit

* `scripts/profile-capture-model.py:raw_source_events` filters away native clocks,
  scopes, scheduler and dispatch. `offline_benchmark` then schedules logical work.
  Neither its timings nor regenerated clocks can qualify N3.0.
* `capture_replay.py:IndexedInputs` preserves every row on disk;
  `offline_scheduler.py:OfflineScheduledReplay` consumes the native scheduling
  and clock tape strictly. Its coroutine stepping measures replay work, not native
  event-loop lateness or real inference IPC. It cannot supply those acceptance metrics.
* `offline_bootstrap.py:restore_cold_engine` correctly requires identical source
  and runtime. The two native W2 captures predate the starting source. No source
  replacement, manifest rewriting or relaxed binding is permitted for native proof.
* `ml/wave2.py` shares engine/session clocks with ordinary processing. Clock rows
  contain method/value/scopeId, not module ownership. Disabling W2 changes the
  call population. Replay cannot skip unidentified reads or fabricate module clocks.
* `cross_venue_public.py` schedules external tasks independently; supplemental
  `venue_gap` has no primary sequence/clock anchor. The streams have independent
  chains but no total scheduler order. Sorting receipt timestamps is not a proof.
* `scripts/smoke-trading-model.py:ShadowProbe` wraps `_evaluate`; a separate 5ms
  heartbeat polls/submits replies. This schedule is not in primary scheduler rows.
  `run_manifest.py:code_provenance` covers scalp_bot and dependency definitions,
  but not this script. A matching engine source hash does not bind this adapter.
* `pipeline_evidence.py` has useful per-worker spans, but market event identity
  and forecast identity are separate; retained-block deltas are not allocation
  throughput. Keep missing joins explicit, never sum independent p99 values.
* `capture_codec.py` combines hash/canonicalization/JSON and only emits batch
  stamps. Codec diagnostics can use the complete preserved row stream with
  separate per-kind phases and separate instrumentation-tax measurement.
* `capture.py:InputWriter` retains 32MiB/32768 limits and one in-flight batch.
  Instrumentation overflow invalidates acceptance. No bound increase is planned.

## Ordered implementation and decision points

1. Commit this audit before code. Inventory full native primary/supplemental
   populations, source/runtime/script binding, clock ownership, and inter-stream
   scheduler anchors. Retain hashes/counts for every kind, including failed data.
2. Reproduce native replay failure using the archived source and original full
   tape. Add regressions before correcting confirmed diagnostic/qualification
   defects. Preserve the original divergence, including sequence, scope and
   expected/actual calls; cleanup must not mask it.
3. Add an executable fail-closed N3 population/preflight report for cumulative
   A core / B segment / C cross-venue / D maker / E labels / F V2 IPC variants.
   It must not turn an incomplete population or a source mismatch into six
   successful logical runs. Unsupported native adapter boundaries are blockers,
   not implicit no-ops. Ordinary decision/portfolio hashes remain distinct from
   input/scheduler/clock identities and unavailable hashes stay null.
4. If native adapters can be fully evidenced, execute all six complete variants
   and join causal stage records. Otherwise retain NOT_TESTED and document the
   exact absent recording contract; do not claim that a preflight is the harness.
5. Measure complete-population per-kind codec phases separately from replay and
   acceptance; preserve all observations in bounded storage. Quantify tax against
   the same batches. Allocation throughput requires a real allocator event source;
   report NOT_TESTED if the available Windows runtime cannot provide one safely.
6. Only measured hotspots with deterministic red/green and exact semantic/chain
   parity can justify a performance patch. Controlled W2.0 stays NOT_MET until
   complete native loop <=20ms / data-to-adapter <=250ms and zero loss are proved.
7. With an open controlled gate: stop market/data/model/Demo expansion. No 900s
   attempt, training, paired market, Demo/private/order, 8h/12h or background days.
   If MET, follow the user's single 900s qualification and native label protocol.

## Validation and evidence

Run focused regression/contract tests, then full Windows preflight, Windows
ML/spawn/codec/CrossVenue/paired/Demo suites, JS syntax and Linux exact-head CI.
Commit implementation separately from evidence/docs. Update HANDOFF, roadmap,
next-stage evidence with exact source/commit hashes, raw inventory, original
failures, remaining gaps and gate graph. A diagnostic or green test does not
promote native latency, natural fills, labels, dataset, model or profitability.
