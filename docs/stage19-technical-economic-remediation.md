> Historical stage document. Its launch/approval wording and tuning values are not the current project status. See [production roadmap](production-roadmap.md) and [HANDOFF](../HANDOFF.md). Existing launchers remain research tools, not evidence of production readiness.

# Stage 19 technical/economic remediation plan

This document converts the 14-point audit after the failed Stage 17 smoke into
an implementation sequence. It is intentionally code-first: no new long paper
run is authorized by this document.

## Phase A — market-object and execution correctness

Status: implemented in Stage 19A.

1. Preserve exact structural-level identity from selection through ARMED/FIRE.
   A breakout zone and its lifecycle/generation metadata now travel as one
   object; proximity matching cannot substitute a neighboring level.
2. Debounce level lifecycle approaches. A distinct approach requires material
   departure, time separation and a later confirmed bar; sub-second boundary
   chatter cannot mature a level.
3. Revalidate pending maker entries every fast evaluation. Cancel immediately
   when the setup disappears, changes side/identity, becomes late/exhausted or
   loses its entry context.
4. Require public-trade-through evidence for maker partial/target fills. A
   book touch alone is not treated as a filled resting order.

## Phase B — one economic lifecycle

Status: implemented in Stage 19B.

5. RiskEngine and PaperBroker use the same slippage-adjusted entry geometry.
   Entry slippage is embedded in expected fill and is not charged twice.
6. Planned partials use the same minimum-net condition as actual paper
   execution. If a 1R partial cannot clear its economic requirement,
   RiskEngine prices the setup without that partial and PaperBroker does not
   take it.
7. Weak-level rejection takes only 30% at the early partial instead of 70%.
   Breakout requires at least 2R gross target room before costs.
8. Positive semantic risk scaling is disabled. Flow/freshness may reduce risk
   but cannot increase it above base structural risk until paper expectancy
   proves a positive edge. The hard net R:R floor remains 1.0.

## Phase C — structural path and market attention

Status: implemented in Stage 19C.

9. Ordinary levels, current-day extremes and previous-day extremes share one
   support/resistance taxonomy across MarketContext, liquidity targets and the
   semantic arbiter. Exact generation identity controls own-breakout
   exemptions.
10. Scanner attention is no longer dominated by already-realized price moves.
    It records direction-neutral opportunity readiness from
    compression -> fresh expansion and remaining move budget.

## Phase D — long-run observability

Status: implemented in Stage 19D.

11. market_context_changed no longer logs every forming-candle/flow twitch.
    Major semantic transitions bypass the throttle; minor churn is compactly
    sampled every 10 seconds by default.
12. Decisions, strategy transitions, risk rejects, research frames and trade
    events retain full causal context, so compaction does not remove the data
    required for post-run strategy audits.

## Phase E — strategy hardening and safe research profiles

Status: implemented in Stage 19E, pending final merge/CI.

13. Trend continuation remains non-tradeable. Density remains evidence-only.
    Weak-level rejection trades the early failed-break + absorption state and
    does not add on late reaction. Breakout cannot FIRE from elapsed hold time
    alone: a no-retest hold also requires directional price response.
14. Daily/previous-day extremes are mature structural obstacles by definition.
    Stage 19 smoke and exact 12h profiles explicitly preserve the current
    disabled strategies/staged-add policy and compact telemetry settings.

## Validation sequence

Do not jump directly to the 12-hour audit.

1. Full repository tests and JavaScript syntax checks must be green after each
   phase.
2. Run a final static technical/economic audit of main.
3. Only then run the prepared 30-minute Stage 19 audit smoke.
4. Review every opened trade as:
   market object -> side -> ARMED -> FIRE -> executable fill -> economics ->
   exit.
5. If the smoke is structurally sound, run the exact 12-hour global audit.
6. The 12-hour audit decides whether rule-based logic is sufficient or whether
   a predictive ML layer is justified. ML is not introduced before that
   evidence exists.

## Current policy

- tradeable: weak_level_rejection, level_breakout
- evidence-only: orderbook_density
- disabled: trend_structure
- staged adds: disabled for breakout and rejection
- absolute planned net R:R floor: 1.0
- structural risk budget: 0.5% equity
- max planned all-in loss per trade: 1.25% equity
- aggregate open all-in risk: 2% equity
- single-position gross leverage cap: 5x
- portfolio gross leverage cap: 10x
- positive risk boost above base: disabled
