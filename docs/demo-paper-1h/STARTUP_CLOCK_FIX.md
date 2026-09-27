# Owner startup clock failure — 2026-09-27

Owner attempt: `G:/scalp-bot/data/audit-demo-paper-1h-20260927-173348`. Authenticated Demo preflight succeeded; the run stopped after about 26.7 seconds with `clock_invalid`, zero pairs, no order commands/fills and both ledgers at 1000 USDT. This is an incomplete attempt, not a one-hour execution result. Original capture and reports are unchanged.

The initial clock request took 459.5151 ms and correctly failed the existing 400 ms bound. The next recorded request took 238.0371 ms and synchronized successfully. The Demo engine incorrectly implemented the ordinary arbiter's pending-entry cancellation as a terminal stop even with no reserved, queued or open execution. That latched Stop before the normal clock retry could restore admission.

An empty controller now waits for a valid bounded clock with admission disabled. Readiness transitions are recorded. The existing retry loop continues and the same 3600-second monotonic deadline includes this wait; no automatic restart or extension occurs. Existing clock/freshness bounds, original market receipts and passport hash are unchanged. Clock loss with any reserved, queued or open execution still halts admission and invokes cancellation/reduce-only shutdown. A recovered clock cannot undo an explicit Stop.

The actual two samples are retained in `tests/demo_paper/fixtures/startup_clock_samples.json` with the original capture hash. Before the fix the reproducer failed because `stop_reason` was already `clock_invalid`; the reserved-execution negative control passed. After the fix, 77 local regressions passed in 12.48 seconds: recorded recovery, mocked complete lifecycle after an initially rejected clock, unchanged deadline, stale-book rejection, pending cancellation, filled-position reduceOnly closure, maker receipt guards and transport recovery. A fixture-only missing receipt-clock flag was corrected without changing runtime bounds; its failed log is retained.

Evidence: `G:/scalp-bot/data/audit-demo-startup-fix-20260927`. No authenticated request, new market connection or experiment restart was performed while diagnosing or testing this fix. The owner supplied the paired-mode command again when asked about ordinary startup; there is no separate ordinary-launch error trace to diagnose. Shared ordinary clock regressions passed.

Use the same owner Start command and passport from `DEMO_PAPER_1H_PROTOCOL.md` after the local checkout is updated. This repair does not establish trading utility or successful completion of a market hour.
