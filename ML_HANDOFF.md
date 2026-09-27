# ML handoff — trained v2, no admission

V1 сохранена и диагностирована: порядок классов/признаков/preprocessing/reload корректен; target_first редок, p_target<0.55 до и после calibration во всех split. Калибровка не создала нулевой выбор. [Диагностика v1](docs/pr58-readiness/abstention_diagnostics.json).

V2 обучена на двух дополнительных dated captures:16790 labels, train6482/cal4160/val3023/test2847/purged278. Actual engine MarketContext восстановил структуру и65.08% известных liquidity observations. Четыре instrument limits подтверждены в собственном contemporaneous bootstrap. 2024 NEAR/XRP исключены: mandatory historical fields неизвестны, September specs назад не переносятся.

CatBoost сохраняет0 selections; logistic2 test labels имеют средний net -0.109132USDT, rule197 — -0.134273. No-trade остаётся контролем. Новые held-out labels были отделены заранее, но даты ранее изучены как bot captures; это не untouched independent regime. Порог0.55/цель30/стоп15/horizon30s не менялись. Веса сохранены до test, exact reload проходит. [Model card](docs/pr58-readiness/MODEL_CARD_V2.md), [команды](docs/pr58-readiness/COMMANDS.md), [dataset/model manifests](docs/pr58-readiness/artifact-manifest.json).

Worker/shadow1200 forecasts выполнен, все abstain, no exchange import/no capital/no orders. Три синтетических exit paths сохраняют обычные решения/ledger. Historical benchmarks отдельно измеряют source/state/features/queue/predict/return/adapter; abstention не называется proposal. Обычный launcher ML не импортирует и прогноза не ждёт.

M2 technical artifact есть; generalization/portfolio utility не доказаны. M3 burst latency admission открыт. Следующий разрешённый шаг — только offline работа по конкретным ограничениям из PR58_READINESS_REVIEW.md. M4/M5, live worker trading, новые market runs и main merge не разрешены.
