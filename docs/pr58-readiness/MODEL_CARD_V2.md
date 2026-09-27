# CatBoost impulse v2 — offline research only

Weights: `G:/scalp-bot/data/pr58-completion/model-v2/model.cbm`; model `catboost-impulse-v2:a9bb5445db534b93`. Canonical dataset: `G:/scalp-bot/data/pr58-completion/dataset-v2-verified`; its dataset bytes are identical to the actually trained `dataset-v2`. The verified manifest corrects aggregate exclusions across both sources (594 transport-censored,116 right-censored), without relabelling or retraining. Local artifacts have no automatic expiration; retain until the owner explicitly deletes them. Weights/datasets are outside the temporary worktree and are not committed. Checksums and environment: [dataset manifest](dataset-manifest-v2.json), [model manifest](model-manifest-v2.json). V1 remains unchanged.

The predeclared fallback uses all eligible activated symbols in two additional September 26 captures. The morning period supplies train; the noon period supplies calibration/validation/test with globally frozen boundaries and complete horizon/60-second bucket purge. The original two-hour development capture is excluded. These are genuinely additional training periods, but the market dates were previously reviewed as bot runs. Test labels were held out before fitting; this is **not a pristine research-unseen external test**. The reserved 2024 test was never downloaded, labelled or used for selection. Mandatory 2024 instrument restrictions remain unverified.

| Split | Rows | Target first | Stop first | Timeout |
|---|---:|---:|---:|---:|
| Train | 6482 | 85 | 582 | 5815 |
| Calibration | 4160 | 24 | 235 | 3901 |
| Validation | 3023 | 14 | 239 | 2770 |
| Test | 2847 | 13 | 124 | 2710 |

278 rows are purged. The 29 test time buckets are dependence groups, not 29 demonstrably independent market opportunities. Full per-symbol/split/epoch coverage and source examples: [context audit](context-coverage-v2.json). Structure known=100%; liquidity known=65.08%, wall fields present=64.54%. A present numeric mask is not evidence that the underlying information is known. Shared production MarketContext definitions are used, including contemporaneous structure, deep-book freshness, trade/OFI warmup and transport invalidation.

Payoff policy is unchanged: 100 USDT nominal, 100ms observation-to-entry delay, 30s horizon, target30/stop15bps, 0.055% taker fee and 1bps slippage each fill. Dated tick/lot/minimum quantity/notional constrain executable labels; gaps and incomplete outcomes are excluded. This is a fixed-plan label study, not strategy stops, learned sizing or a portfolio. No leverage/partial/ordinary threshold changed.

CatBoost: one fixed 100-iteration depth4 candidate, seed1729, one CPU thread. Logistic C=1 is the second learned baseline. Preprocessing fits train only; independent calibration temperature is 0.772076. Weights, metadata and fixed p_target>=0.55 action policy are saved before test evaluation. No alternative threshold was tuned. Exact reload passes.

| Test control | Selected | Mean selected net USDT | Logloss |
|---|---:|---:|---:|
| CatBoost | 0 | unknown/no selection | 0.156790 |
| Logistic | 2 | -0.109132 | 0.158410 |
| Fixed impulse rule | 197 | -0.134273 | 0.619447 |
| Train prior | 0 | unknown/no selection | 0.228134 |
| No trade | 0 | zero total cash change | n/a |

CatBoost Brier=0.080629, ECE=0.007114. These calibration metrics do not establish a profitable trading action. Maximum calibrated p_target on test is 0.05156, and it is below0.55 even on train (max0.14401). V2 continues to abstain. Validation average precision is only0.01734; no economic case for lowering the threshold was established. Timeout payouts are included, not treated as zero or as target success.

Historical shadow executed 1200 forecasts, all probability abstentions, no drop/failure, clean Windows-spawn shutdown and no exchange imports. Three synthetic ordinary execution paths preserve exact decisions/ledgers. End-to-end adapter latency and actual emitted-proposal latency are reported separately; this model emitted no proposal. Performance benchmark reports do not grant trading authority.

Decision: trained technical candidate, **not admitted for trading**. Independent regimes, untouched external test, portfolio utility and robust burst latency remain unproven (adapter p99 exceeds250ms in burst). No live market run, ML orders or main merge is authorized by this result.
