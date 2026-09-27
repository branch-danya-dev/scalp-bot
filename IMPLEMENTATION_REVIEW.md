# Implementation review — 27 сентября 2026

Реализованы параллельная подготовка стратегий, единый допуск исполнения, дополнительные
transport recovery исправления и реально обученный локальный ML-кандидат с worker/shadow.
[Draft PR #58](https://github.com/branch-danya-dev/scalp-bot/pull/58).
Это draft: ограничения политики, данных и задержки ниже остаются открытыми.

## Версии и неизменность источника

| Ветка/источник | Проверенный SHA |
|---|---|
| baseline main, G:/scalp-bot | 3403b03011a5e89f6f0c4ecb03c6f76df4b7f1e1 |
| PR #57 | f0364a506b8375398c62e98fa7caa5bded640674 |
| PR #54 | c45c7b4519deda5b6e792d2bb57627376d4b19bd |
| integration merge | b28ff1f |

Heads получены fetch перед интеграцией, baseline был чистым и остался чистым.
Worktree отдельный: C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot.
Ветка codex/parallel-scenarios-ml-v1. Установка ML выполнена в её .venv; parent/child
import path проверен. Main, чужие ветки, audit worktree и исходные зависимости не менялись.

Capture current-12h-20260926-222654-704-f604f5f6, id790eebd906ac4a41baf69eccb5b69ae3:
2:07:48.188, шесть сделок, −40.732119 USDT. Повторный SHA256 исходного gzip:
`caf9cf7c293faab93d3832a96b52298beb12da4316ed55c2f414e346dd337a6d` — совпал.
Hash chain читается, seal incomplete из-за transport tail. Footer не дописывался,
полный replay не объявлен успешным. Старые TWO_HOUR_RUN_REVIEW/PATCH_REVIEW сохранены.

## Поведение и коммиты

- Интеграция #54/#57; 1075ef5 — consumer failure и потеря готовности книги.
- 1cf62b8 — independent scenarios/common admission, schema3/UI, regression matrix.
- 6eace29 — quote_tape_v1 исследовательское подтверждение, default legacy.
- 69ea4eb — raw capture→features/labels/splits, заранее закреплённый CPU experiment.
- Последующие логичные коммиты закрепляют capture compatibility, обученные артефакты,
  worker/shadow и финальный отчёт; полный список: git log main..HEAD.

Общая оценка рынка выполняется один раз, затем стратегия видит собственный scenario.
WAIT, exception и локальный risk reject не захватывают символ. Полный свежий план
допускается без окна ожидания: earliest firstSignal, затем breakout/rejection/trend/BETA.
Общий риск и капитал прежние. RESERVING/pending/position/cancel/reconciliation заняты;
чужое сопровождение, второй депозит и независимые позиции на символе не введены.

Профиль и плечо не переключались. [Архитектура и миграция](docs/architecture/parallel-scenarios.md).
Сохранены три исправления #57: новый sweep не наследует XRP absorption/fire,
ARMED не омолаживается через WAIT, close_timeout укладывается в shutdown budget.
Consumer error теперь обнаруживается без следующего пакета; новый transport epoch
сбрасывает недостоверный fast/deep контекст и pending через штатную отмену.
[Транспорт: measurements и недостающая диагностика](docs/implementation/TRANSPORT.md).

## Выполненные проверки

| Проверка | Результат / доказательство |
|---|---|
| Объединённый исходный набор | 1344 passed, validation/integrated-baseline.txt |
| Полный конечный preflight | 1371 passed, 226.96 с, validation/preflight-final.txt |
| Старые single-owner expectations | Обновлены на независимый context; свечи/flow/ownership сохранены; 87 passed |
| XRP old absorption, frozen ready/WAIT, shutdown | Сохранённые регрессии #57 проходят в полном наборе |
| Все шесть исторических broker controls | T01–T06 проходят, прибыльный partial и исходный ETH stop сохранены |
| Same/opposite ready, WAIT всех четырёх владельцев | Один победитель/резерв, локальная ошибка и отказ изолированы |
| Shared risk, cancellation, late fill, stale confirmations | Старые lifecycle/risk/execution регрессии + новые parallel cases |
| Capture/replay | 41 targeted passed; v4 config reader preserved, v5 research field и scoped reset |
| Raw→features | Причинность, future/PnL independence, gaps, actual engine feature parity |
| Train/reload | CatBoost фактически обучен, exact reload, checksum tampering rejected |
| Worker faults | Реальный Windows spawn hung/crashed, bounded mailbox, metadata/NaN/TTL/duplicate tests |
| Исторический shadow | 200 forecasts, 200 abstain, чистое завершение, Bybit не импортирован |
| Обычный движок off/shadow | 15 decision/execution events и ledger идентичны; 1 synthetic partial+stop |
| GitHub CI | Linux **1369 passed, 2 skipped**, Windows ML **223 passed**; [полные CI logs](https://github.com/branch-danya-dev/scalp-bot/actions/runs/36282268800), validation/ci-*-excerpt.txt |

Пути validation в таблице относительны docs/implementation/. Первые неуспешные
логи сохранены: manifest config classification, затем scope parent и пять старых
contract expectations. Они не выдаются за зелёный прогон. Прибыльные ожидания
не удалялись и не менялись. Модуль ML не подключён к обычному launcher.

## Модель и данные

[Команды](docs/ml/experiments/COMMANDS.md), [model card](docs/ml/MODEL_CARD.md),
[dataset manifest](docs/ml/experiments/dataset-manifest.json),
[model manifest](docs/ml/experiments/model-manifest.json), [evaluation](docs/ml/experiments/evaluation.json).
10 225 labels всех семи активных монет, включая обычные/ложные окна; не выборка сделок.
CatBoost 100 depth4 + logistic + fixed impulse rule + train prior. Test logloss
.419513/.500817/.730969/.621738 соответственно. CatBoost при p_target>=.55 выбрал0;
порог не подбирался. Timeout имеет денежный исход, costs учтены на исполнении.
Только одна уже просмотренная сессия: полноценный independent holdout отсутствует.

Веса и raw не в Git, локальные data/ml/impulse-v1-final и models/ml/impulse-v1-final,
retention до удаления владельцем. В Git manifests, source/hash/coverage/split/class,
preprocessing/calibration, seed, exact версии среды и численные отчёты.
Полезность прогнозов на новых периодах и portfolio delta-net не доказаны.

## Незавершённое и границы доказательств

1. Quote+tape кандидат реализован отдельно, выключен. Учтены все81 rejection scenarios,
   11 ready, включая6 неисполненных, и все32 ready markouts обеих стратегий. Но точное
   сравнение обеих политик с последовательным восстановлением первого reclaim **не
   завершено**. Lost winners/new entries/delay/remaining move/portfolio PnL неизвестны.
   [Исследование выплат и ограничение](docs/implementation/REJECTION_POLICY.md).
2. Backpressure воспроизведён и его обнаружение исправлено; происхождение всех семи
   failed handshakes не установлено. DNS/TLS/connection-phase diagnostics отсутствуют.
   close_timeout не считается лечением внешней сети.
3. M2 — технический кандидат. Нет независимых периодов, external историческая lot/tick/
   multiplier/coverage не подтверждены; structural/liquidity признаки missing.
4. M3 — рабочий offline shadow, не полный performance допуск. Готовые features→proposal
   p99=3.26мс; synthetic evaluate→worker→adapter p99=64.33мс, 8 forecasts. Event-loop
   p99 off54.28/shadow53.57мс превышает заранее заданные20мс. Предел не смягчался.
5. Полный counterfactual portfolio routing/policy/ML PnL не вычислен. Исчезновение
   старых проигрышей не считается улучшением. Синхронный CPU-hung strategy остаётся
   непрерываемым; новый отдельный поток для каждой стратегии не вводился.

Новые market/paper/live прогоны, реальные заявки, включение ML trading и merge main
не выполнялись. Issue55 оставить открытым. Следующий допуск только отдельным решением.
