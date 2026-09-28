# DemoExecutionCalibration — future execution qualification

Status: NOT_TESTED. `launchAuthorized=false`; no Demo orders are authorized by
this protocol. The current implementation is an offline ledger/contract harness.
Mainnet is forbidden. Existing historical Demo permissions do not apply.

Prerequisites: W2.0 complete/native latency and integrity, real causal current
PreparedIntent/EconomicPlan/FIRE/fill/managed exit, native label replay, promoted
multi-period dataset, frozen V3 artifact, concrete paper control parity and a
subsequent explicit owner authorization. No current task code opens credentials,
starts private sockets or calls an order endpoint.

Use existing `demo_paper.contracts.Intent.freeze`: one immutable causal intent,
one geometry/quantity/risk identity, independent PaperVenue and DemoVenue ledgers.
The calibration collector observes both; neither ledger's fills feed the other's
matching. Commands preserve instrument quantity step, price tick, minimum/maximum
constraints, USDT fee denomination and reduceOnly protection. Paper and Demo
command IDs differ but bind the same pair and revision.

Record every command send, acknowledgement, cancel/amend request and result,
partial/full fill, remaining quantity, maker/taker flag, actual fee, price, source
and receipt time, protective stop/target/reduceOnly action and reconciliation.
Report paired fill-price/slippage and quantity differences, actual cost differences,
order-to-ack and order-to-first/final-fill p50/p95/p99/max. Never substitute REST
ack for fill, cancel ack for cancellation, or missing fee/exposure for zero.

## Prospective short qualification criteria

- Select one 30–60 minute interval before Start, no retries/extensions for fills.
- At least 30 natural strategy-owned intents and 20 paired fills across >=3 symbols;
  otherwise INCONCLUSIVE. Synthetic fixtures do not count.
- Dedicated Demo account identity, exact account mode/margin/position mode and
  captured instrument constraints; no foreign orders, executions or positions.
- Clean private WS application heartbeat, all executions deduplicated and reconciled,
  no unknown fee currency, no precision violations and zero unknown exposure.
- Record partial/cancel/amend/stop/target/reduceOnly race coverage. Missing observed
  cases remain NOT_TESTED; do not synthesize orders solely to make natural coverage.
- Bounded 90-second finalization: block new entries, cancel owned pending orders,
  close owned exposure with valid reduceOnly, reconcile both ledgers. Any unconfirmed
  order/execution/account reconciliation is NOT_MET with raw state retained.
- Execution differences require review against the frozen Paper assumptions.
  This qualification does not test alpha, profitability or V3 promotion.

After independent evidence, change the execution model's slippage/queue/fill
assumptions through a separate reviewed patch with parity tests. Never tune
strategy thresholds from a single execution discrepancy.
