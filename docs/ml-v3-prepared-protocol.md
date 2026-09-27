# ML V3 — pre-registered research protocol

V2 weights, 0.55 threshold and 30/15 bps / 30s adapter are unchanged. No V3 model is trained or enabled by this change. V3 is a ranker/veto of strategy-owned intents, not an independent side generator.

## Sampling and provenance

Use `PreparedDatasetCollector.observe` from the engine's causal first-prepared boundary. Deduplicate symbol/owner/side/scenario/episode (setup is only the fallback identity without an episode); a renamed setup does not authorize resampling. Do not refresh features after economics rejects or after a later price makes the setup look better. Freeze the same `MarketContext`/FeatureSnapshot and explicit missing-coverage masks. Include economically rejected intents in the observation population; mark their economic outcome separately and do not count them as executed trades. Save source sequence, capture/config/source/model hashes and clock domain. Transport gaps invalidate pending labels, never become zero returns. Monotonic clocks from different runs must not be concatenated.

## Plan and labels

Primary plan is the owner's frozen structural entry/stop/target ladder, priced by the unchanged EconomicPlan (fees, slippage, stop-depth stress, quantity and partial/runner policy). Do not rebuild a target from future extrema. Fixed V2 30/15/30 is a named comparator only. Training-only analysis of executable MFE/MAE and costs may propose a small alternative policy family; freeze its candidate list, implementation hash and selection objective before reading validation. No target/threshold search on validation or test.

Available development evidence does not justify a universal 30 bps target: in PR58 parallel_legacy the median 60s top-quote MFE was 15.53 bps for 24 covered breakout ready events and 2.74 bps for 12 rejection ready events. These are quote-only bounds at a different horizon, without quantity/depth or maker-fill proof. Actual closed-position median MFE/MAE was 0.493/0.146 R for breakout (n=3), and 0/1.009 R for rejection (n=5); median conditional winner-cost share was 15.39%/19.97%. The observations support changing the research question, not fitting replacement numbers. Full distributions and their limitations are retained in the historical evidence report.

Produce separate outputs: target-before-stop probability, realized net R, executable MFE/MAE, time to first event and censoring reason. Entry requires the same observed latency/depth/quantity and maker fill evidence as the paper executor. Future bid/ask alone cannot assert a maker fill. Preserve partial/stop/no-follow-through events and actual lifecycle costs. Quote-only bounds are diagnostics with an explicit scope, not training outcomes or portfolio PnL. End-of-window, transport/depth gaps and open positions are censored. Never add overlapping independent labels into portfolio returns.

## Validation fixed before training

Global chronological wall-time folds, rolling training windows, a separate calibration interval, then validation. Purge any training label extending to the boundary; embargo 60 seconds on both sides and purge entire causal episodes shared with evaluation. Use `purged_walk_forward` with complete future label end timestamps, not observed exit alone if MFE/MAE extends beyond it. Keep the final untouched capture as test; the PR58 development captures are not that test.

Run each fold both pooled and leave-one-symbol-out. Report symbol/regime counts, uncertainty, calibration/Brier score, net-R ranking curves, coverage/abstention, cost distribution and concentration (largest-symbol share). Compare against unranked eligible setups on identical captured membership and common PortfolioRisk, with separate portfolio replay. ENA-only improvement is insufficient. Fit normalizers, feature selection, plan candidates and veto thresholds exclusively on training/calibration. Cluster uncertainty by capture/episode, not overlapping labels.

## Runtime boundary and promotion

`PreparedForecast` contains source/intent identity and economic estimates; it contains no order size, side override, broker or secret. `forecast_matches` rejects wrong identity, source, model or expiry. A future adapter may rank within the set that passed strategy/context/ownership checks or veto it; it cannot approve a rejected setup. Every survivor must rebuild EconomicPlan against current PortfolioRisk immediately before FIRE. No ML threshold is activated by this protocol.

Before promotion: completed labels with depth/fill provenance; multiple independent captures and nontrivial samples per evaluated segment; purged walk-forward and symbol holdouts; concentration/cost stability; then a short capture-mode technical smoke. Lack of observations is inconclusive, not success. Model artifacts remain absent until these data gates are met.
