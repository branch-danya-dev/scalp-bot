# Повторный анализ репозитория для ML

Проверено дерево main `3403b03` / `a5781b1659f962b2c0724633c3b93de37f5d0cf9`.
Это анализ кода и документов, не аудит новых событий текущего 12h.
Источник каждой строки ниже — указанный модуль на этой базе.

| Область | Уже существует | Что нужно для ML, но пока не реализовано |
|---|---|---|
| Scanner / universe | `engine.py`: bootstrap, активация/деактивация, общий список монет | Epoch активации в сообщениях ML; удалённая монета не принимает старый прогноз |
| Формирующаяся свеча | `strategy/pre_state.py`: pace, expansion, velocity, microMove5s/15s, возраст источников | Чистый версионированный feature adapter; одна семантика offline/online |
| Flow / OFI | `strategy/flow_context.py`: горизонты 5/15/60s; `engine.py`: book_flow | Отсутствие признака не превращать в ноль; контроль фактического покрытия |
| Плотность | `strategy/density.py`, `liquidity_evidence.py`: состояние выбранной стены, атаки/истощение/пополнение | Это не карта всей ликвидности и не обученный predictor; модель использует наблюдаемое покрытие |
| Структура / режим | `structure.py`, `regime.py`, `market_context.py` | Отделить наблюдаемые уровни и категориальные оценки от вероятностного прогноза |
| Один владелец | `scenario.py`, `scenario_runtime.py`, `scenario_identity.py` | Будущий импульсный MarketObjectRef, ML proposal и атомарная арбитрация источников |
| Планы / экономика | `domain.py`: StrategyDecision/TradePlan; `risk.py`, `strategy_policy.py` | ML adapter с теми же единицами/издержками, но собственной причинной инвалидацией |
| Исполнение | `execution_book.py`: coherent_execution_book; `paper.py`: fills, partial, funding | Не создавать отдельный ML-брокер; одинаковое исполнение baseline/candidate |
| Полный входной журнал | `input_journal.py`: sequence, processingMonoNs, hash chain, bootstrap/scanner/control/scope | Потоковая выборка с указанием источника каждого snapshot; текущий журнал не изменять |
| Baseline replay | `capture_replay.py`: IndexedInputs + verify_capture; `offline_scheduler.py` | Это точное воспроизведение старого пути, не автоматическая симуляция новой стратегии |
| Сравнение портфелей | `e01_comparison.py` поддерживает только E01/E06 и явные callbacks, требует event-driven=False | Нельзя выдать его за готовый ML-runner для текущего event-driven профиля; нужен адаптер |
| Research dataset | `research_dataset.py`: агрегаты trades/hindsight/interactions/arbiterBlocks | Не готовая unbiased обучающая таблица; hindsight не может быть входным признаком |
| Наблюдаемость | `latency_observability.py`, журнал решений, UI | Задержки source→features→worker→proposal; версии и причины неиспользования ML |

## Существенные выводы

1. Переписывать обработку биржи и весь движок не требуется. Но одной регистрации
   ещё одного `Strategy` недостаточно: router выбирает владельца до его сигнала,
   а параллельному ML-предложению нужен отдельный, не блокирующий путь допуска.
2. База current-12h включает breakout/rejection, density evidence-only,
   trend/BETA выключены (`.env.paper-capture-current-12h`). ML сейчас отсутствует.
   Классификация bearish_impulse сама по себе не создаёт импульсный ордер.
3. Набор доступных признаков достаточен для начала исследования. Достаточность
   качества/объёма обучающих данных и статистический edge не установлены.
4. Старые строки README о повторных semantic veto и ранние roadmap-статусы могут
   расходиться с текущим `_owned_assessment`. ML-документы ссылаются на код этой базы;
   исторические документы не переписаны задним числом.

## Что именно доступно и чего нет

Для локального анализа восстановлено точное дерево из ранее полученных GitHub
artifacts и опубликованных patches; `git write-tree` совпал с tree main. Полная
Git-история и многогигабайтные raw-сессии этим не восстанавливались. GitHub main
прочитан отдельно. Нет проверенного завершённого current-12h, его input journal,
manifest/фактических задержек и отчёта capture-check. Поэтому в M0 не проводится
обучение и не назначается число «достаточных» samples по шести контрольным сделкам.

## Подготовка источников

После штатного Stop/sealing сначала проверить старую запись на исходниках и
зависимостях **самой записи**, до обновления рабочей копии. Сохранить capture.json,
capture-manifest.json, session JSONL, inputs gzip, source-at-capture.zip,
capture-check.json и terminal.log. Raw/weights/datasets не коммитить.
Повреждённый префикс годится лишь для явно ограниченной диагностики; не дописывать
footer и не называть его полным holdout. Уже анализированные периоды считать
development, а не независимым подтверждением.
