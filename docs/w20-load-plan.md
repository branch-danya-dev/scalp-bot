# W2.0 continuation: bounded capture load repair

Starting source: PR61 `a84ee88633a8af1f8acc529e81270c9e25da275a`, stacked on PR60 `1a2d0f67da0331df7d2d134bffde6f64141c7898`. Main/PR60 remain untouched.

## File audit before changes

- `scalp_bot/capture.py:InputWriter`: each input row independently resolves its canonical hash, JSON encodes, calls gzip write, and releases byte accounting. Queue remains bounded to 32 MiB / 32768 rows. Small writes and GIL contention are hypotheses, not yet established causes.
- `scalp_bot/input_journal.py:InputJournal.append/DeferredJournalRow.resolve`: validation and detached memory accounting run on producer; canonical JSON hashing runs on the FIFO writer. The schema, canonical hash bytes, every clock/scope row and ordering must remain unchanged.
- `scalp_bot/recorder.py`: separate bounded session writer; critical loss invalidates evidence. Do not raise queue limits or reduce observations to make performance pass.
- `scripts/profile-capture-model.py`: historical load profiler omits captured clock-read overhead; cannot alone reproduce the baseline input-writer load.
- `scripts/smoke-trading-model.py`: bounded public/paper smoke with frozen V2 load probe and optional unified W2 observer. Current result has no writer high-water/drain or stage timing evidence. Inspect stop-on-recording-failure behavior before rerunning.
- `tests/test_journal_writer_pipeline.py` and `tests/test_paper_capture.py`: hash/detach/order/overflow/replay coverage exists; bounded batching/error/shutdown coverage will be required if batching proves useful.

## Implementation and evidence sequence

1. Preserve original failed baseline. Summarize input-rate/burst distribution and profile the real InputWriter on fixed recorded rows, including clock/scope events. Record raw receipts and source hashes. Distinguish writer throughput from full-engine latency and market/network causes.
2. For each confirmed defect, preserve a failing regression before implementation. If small gzip writes limit throughput, implement a bounded FIFO batch with byte accounting retained through persistence, unchanged hashes/queue limits, finite shutdown, and explicit loss/error reporting. Add low-cost queue high-water diagnostics only if needed for validation.
3. Compare before/after on identical offline workload, validate exact decoded row/hash equivalence, and run capture/journal/causal replay regressions. Investigate any remaining loop bottleneck using the same captured population, without tuning strategy/risk.
4. Run full preflight. Freeze source/config/runtime/model hashes before one 300s public-feed paper smoke with unified W2 collection and the existing V2 load probe. No automatic retry, extension, relaxed gates, Demo or mainnet. Abort new activity on capture failure and retain raw/failed data.
5. Validate primary/supplemental chains, queue/drop/backpressure and latency, inspect any natural prepared→economic→FIRE→fill→exit path. Zero fills stays INCONCLUSIVE. A failure stays NOT_MET and is not hidden by a later quiet period.
6. Update HANDOFF, roadmap/evidence and this PR. Do not advance training, maker execution, registry enforcement or paired/8h A/B without their existing evidence gates.

## Profile-directed extension

The fixed 25045-event/58s load reproduces byte overflow and failed latency without network. Batching alone still overflows (receipt retained). The diagnostic profile identifies `strategy/common.py:compute_trade_flow` as the largest pure processing cost and repeated detached scenario copies as another cost. Unroll the fixed three window buckets while preserving exact order and built-in sums; remove only the second copy in `ParallelScenarioRouter.context_for`, with nested detachment tests. Reuse an identically configured stdlib JSONEncoder in `manifest_validation.fingerprint`; preserve canonical bytes for ASCII/Unicode/floats and concurrent callers. No observation omission, clock sampling reduction or strategy threshold change.

## Subsequent findings and bounded implementation

- The first instrumented load helper wrapped engine/broker clocks but omitted clocks of sessions created during warmup. Those partial-clock runs are retained and explicitly separated from full-clock evidence. The reusable profiler now wraps all three; all 467402 observations are required on the fixed full workload.
- Small writer batches and gzip-only isolation still allow parent hash/JSON work to contend with the engine. The final pipeline sends one bounded detached batch to a spawned hash/JSON/gzip codec. It returns per-chain receipts before the next batch; the parent preserves order and retains byte charges through file write. Standard concatenated gzip members decode to the same JSONL and canonical hashes. No extra unbounded queue is introduced.
- Full-clock attempts still exposed allocation/GC pressure. Clock observations now use scalar deferred records; the writer constructs their JSON containers. Only owned acyclic capture structures use `msgspec.Struct(gc=False)`; global runtime GC and every recorded observation remain unchanged. Existing conservative memory charges are preserved.
- Experiments with an alternate GIL interval, handwritten clock hashing, gzip-only child and a large thread-only codec were not retained. Every failed run/report/raw remains in `work/w20-load`.
- Smoke monitoring previously continued to its deadline after writer failure; regression-first repair stops new activity and retains the failed capture. Transport diagnostics bypass `_emit`, so the smoke also observes canonical transport journal events and invalidates evidence for backpressure/discarded messages. This defect has a separate red/green receipt.
- Passing writer throughput does not imply latency success. Full-clock load still fails the predeclared loop/adapter budgets. The next public 300s paper capture is bounded integration evidence, not authorization for a paired/long run or a claim that W2.0 passed.
