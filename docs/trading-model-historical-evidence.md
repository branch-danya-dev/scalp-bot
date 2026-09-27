# Historical setup evidence — previously studied PR58 captures

Input: `data/pr58-completion/{A,B,C}/{A,B,C}.jsonl` and `data/audit-pr58-trade-plan/trades-v3-entry-snapshot/all_ready_controls.csv`. Original files are read-only. A/B/C are alternative replays of overlapping observations, not independent experiments or additive portfolios.

Closed-position reconciliation:

| Replay | Closed positions | Portfolio net USDT |
|---|---:|---:|
| single_legacy | 5 | -34.367091 |
| parallel_legacy | 8 | -43.206377 |
| parallel_quote_tape | 8 | -43.530032 |

In parallel_legacy, breakout: 3 closed positions, net -1.439775. Rejection: 5 closed positions, net -41.766602; all five were countertrend relative to their entry-time local regime. Each fine-grained segment has only one observation. No segment has enough evidence for the new default sample requirement of 100. `quote_tape_v1` did not improve this portfolio.

Separate causal ready-control population, parallel_legacy:

| Strategy/local relation | Ready | Covered 60s quote path | Positive illustrative net60 |
|---|---:|---:|---:|
| breakout/trend aligned | 22 | 21 | 7 |
| breakout/non directional | 3 | 3 | 0 |
| rejection/countertrend | 11 | 11 | 0 |
| rejection/non directional | 1 | 1 | 0 |

These 60-second controls use top executable quotes and illustrative costs; they do not establish depth, maker fills, partial/runner lifecycle, complete MAE or net R. Missing metrics remain null. They are never summed into portfolio PnL. Current scripts retain strategy × trend relation × local regime × HTF × flow × target source × stop-distance × cost-share dimensions; absent historical fields stay unknown.

Actual closed-position excursions in parallel_legacy: breakout median MFE/MAE 0.4929/0.1457 R (n=3), rejection 0/1.0086 R (n=5). Conditional winner-cost share median is 0.1539/0.1997 respectively. Covered ready quote-path MFE median at 60 seconds is 15.5314 bps for breakout (n=24) and 2.7436 bps for rejection (n=12); illustrative net60 median is -9.5397/-23.4684 bps. Quote MFE can be negative when every executable exit quote remains below entry after spread. These numbers do not establish a new 30-second ML target/stop or a replacement horizon.

Decision: keep rejection available with segment expectancy in shadow by default. Countertrend rejection deserves prospective controlled comparison, but these few previously studied outcomes do not authorize a fitted hard filter. Enforce mode is explicit, sample-limited, and based only on unique completed positions. Breakout's local-direction preference is unproven; this ready-control sample contains no local-countertrend breakout, so it cannot validate that subgroup. Restored flow/HTF admission applies regardless of scenario routing, with the existing confirmed 1h-only exception.

Reproduce closed-position report: `python -m scalp_bot.setup_segments <A.jsonl> <B.jsonl> <C.jsonl> --output <report.json>`.
Reproduce separate ready-control report: `python scripts/analyze-pr58-setup-segments.py <data-root> --output <ready-report.json>`.

This evidence is historical and pre-change. It is neither post-fix profitability nor an untouched holdout.
