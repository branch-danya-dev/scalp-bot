# ML handoff — 27 сентября 2026

PR #54 M1b1 интегрирован с #57 в codex/parallel-scenarios-ml-v1. Current-12h
остановлен и проверен аудитом; baseline G:/scalp-bot не изменён. Модель реально
обучена: CatBoost 100 depth4 + logistic/rule/prior controls, 10 225 causal labels,
глобальные train/calibration/validation/test с purge. Данные и веса — локальные
*-final артефакты, точные пути/hash в [model card](docs/ml/MODEL_CARD.md).

[Выполненные команды](docs/ml/experiments/COMMANDS.md) включают build, train,
evaluate, predict и shadow. На test CatBoost logloss .419513 против logistic .500817
и rule .730969, но при frozen threshold он воздержался от всех входов. Это первый
технический кандидат на одном просмотренном периоде, не доказанная модель торговли.

Worker Windows spawn не импортирует биржевой клиент, очередь ограничена, основной
бот не ждёт ответов. Исторический shadow200 и synthetic off/shadow сохранили
ordinary ledger; ни капитала, ни полномочий ML нет. Event-loop бюджет20мс не пройден.
M2 generalization/M3 performance остаются открыты; M4/M5 не выполнялись.

Дальше нужны подтверждённые metadata/coverage внешних независимых периодов и
достаточная нагрузочная проверка. Новых рыночных прогонов/ордеров/main merge нет.
[Архитектура](docs/ml/ARCHITECTURE.md), [обзор реализации](IMPLEMENTATION_REVIEW.md).
