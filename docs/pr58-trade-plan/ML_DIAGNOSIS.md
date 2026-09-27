# ML V2: fixed-plan diagnosis

Analysis uses train/calibration/validation only (13665 windows). Published test2847 and purged278 are excluded from new prediction/ranking containers. Existing dataset/model/policy hashes match; no fitting, calibration, barrier, size or threshold change. Raw production OrderBookState reconstruction matched original entry/exit events and first barriers for all13665 development labels. Later excursions after stop are separate diagnostic bounds, not successful original plans.

## Validation economics

| Outcome | Windows | Mean gross | Mean fees | Mean net USDT |
|---|---:|---:|---:|---:|
| All | 3023 | -0.027686 | 0.104004 | -0.131690 |
| Timeout | 2770 | -0.016740 | 0.103604 | -0.120343 |
| Stop first | 239 | -0.174510 | 0.108395 | -0.282906 |
| Target first | 14 | 0.313003 | 0.108282 | 0.204721 |

81 timeout windows are net-positive and2689 negative; timeout is neither zero cash nor automatically a failure. Both entry and exit slippage are already embedded in fills. Fees are deducted once; depth, dated tick/quantity/minimum constraints are inherited from the unchanged label engine. Real-row arithmetic identities and long/short synthetic profitable and losing timeout examples pass.

The30bps target is a median6.0345 times the pre-entry forming range on validation. Median favorable executable quote movement before the original exit is0.0291bps; p95 is13.4665bps, below30bps. This is a statement about the fixed-grid, both-side sample and costs, not proof that the instrument never offers an impulse. Target-first outcomes are genuinely profitable on average, but extremely rare here. Conditional class payouts vary by symbol/state; p_target is not expected net.

## Economic ranking

Predeclared validation deciles and overlapping top1/5/10% show no useful positive selection. CatBoost top1%:31 windows/11 dependent time buckets, mean net -0.157506; top5%:152/23, -0.143500; top10%:303/27, -0.143044. Whole validation: -0.131690. Upper decile has more target outcomes (2.318% vs0.463% overall) and more positive windows, but worse mean net. It is96.03% concentrated in ENAUSDT; top5% is entirely ENAUSDT. This separates rare-target ranking from money ranking.

Every CatBoost decile has a negative mean. Group-bootstrap intervals for the declared top slices remain negative, but the29 global60s buckets are dependence groups, not demonstrably independent experiments. The short single-capture validation and symbol concentration limit generalization; neither small logloss nor these intervals establish deployment utility.

Logistic top10% mean -0.141192; fixed rule at its unchanged0.55 selects258 windows, mean -0.147801. CatBoost and logistic select0 at0.55. No-trade changes cash by0. Prior and fixed-rule rank bins have ties and chronological tie-breaking; their within-tie ordering is not a predictive signal. No sum of overlapping windows is reported as portfolio PnL.

Detailed split/symbol/side/capture/outcome and frozen pre-state partitions (flow/move/range/spread/depth/liquidity-known) are in ml_plan_diagnostics.csv; scores, uncertainty and concentration in ml_score_payoff.csv. Test was not used to design groups or choose a policy.

## Data and decision timing

The diagnostic sample remains a10-second grid in both directions. Among26 causal first-ready events in the development captures, only3 have a preceding retained sample within1second;23 have an older or absent sample. This demonstrates timing mismatch, not loss of every impulse state or profitable missed entries. These events were selected by causal readiness, not future price movement. Feature definitions/coverage remain those of the running bot; unknown liquidity is an explicit group. Historical2024 instruments with unknown mandatory restrictions are not admitted.

Conclusion: fixed-grid plan suitability, transaction costs, weak economic discrimination and limited regime evidence all contribute. V2 can rank rare target events without providing a net-positive action. Keeping its abstention at0.55 is consistent with these findings. No trading utility or portfolio improvement is established.
