# Preregistered V3 population gate — 28 September 2026

Policy: `wave2-population-20260928-v1`, implemented in
`scalp_bot/ml/dataset_promotion.py`. Freeze its canonical policy hash before any
future capture/model result. Existing development captures are not untouched test.
Changing this policy requires a new prospective version and new independent data;
do not relax it after fitting. These are research sufficiency floors, not proof of edge.

| Requirement | Fixed minimum / maximum |
|---|---:|
| First-prepared observation population, including economic rejections | >=2000 |
| Complete executable labels | >=1000 |
| Complete breakout / rejection labels | >=200 each |
| Symbols | >=6 |
| Independent captures / periods | >=6, each >=10 minutes |
| UTC dates | >=3 |
| Largest symbol / capture share of complete labels | <=30% / <=25% |
| LOSO-supported symbols | >=4, each >=100 complete labels across >=3 captures |
| Local regime / HTF bias / flow alignment | >=2 known categories each, >=100 labels/category |
| Missing context share in each dimension | <=10% |
| Censored share of economically eligible prepared intents | <=35% |
| Exact frozen plan, source, instrument, executable entry depth and fill/cost ledger | 100% complete labels |

Overlapping captures and captures separated by <=180 seconds count as one market
period. Global local-UTC intervals define splits; monotonic clocks remain confined
to their own capture. Transport/depth gaps produce null outcomes with reasons.
Economic rejections remain observations and never become executed trades or zero R.

External-venue missingness does not prevent Bybit-only training or safety execution.
For a later cross-venue influence decision, additionally require >=70% both-venue
fresh coverage and >=100 complete labels per aligned/neutral/opposed cohort, for
each strategy, on the identical prepared population. Otherwise telemetry-only.

All public fit/development/final-test entry points recompute this population gate
and exact primary/replay/source/config/runtime evidence before numerical fitting.
Synthetic numerical unit tests exercise the private estimator primitive only.
Reserve final-test captures before collection; they never enter development
selection, preprocessing, calibration or threshold choice. Apply the same population
floors separately to development and untouched test; a small test is inconclusive.

Learning remains Logistic/Ridge and CatBoost. Purge entire episodes, including
future embargo observations, and retain >=60 seconds embargo plus disjoint
calibration. Freeze dataset/splits/schema/model/calibration/adapter hashes before
runtime smoke. Economic rank stability across captures/symbols/regimes, LOSO,
untouched test and same-PortfolioRisk replay remain additional promotion gates.
