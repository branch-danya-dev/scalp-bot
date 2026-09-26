# ML handoff — отдельное направление

26 сентября 2026. Ветка `feat/ml-impulse-foundation`, база main `3403b03`.
[Документация](docs/ml/README.md) · [План](docs/ml/ROADMAP.md).

Пользователь поручил повторно проанализировать репозиторий и начать локальную
модель импульсного продолжения. Основной current-12h, по его сообщению, идёт;
его результат пока не получен. Обычный HANDOFF и назначенный capture не отменяются.

M0 сохранён. M1a: добавлены изолированные архивные readers Bybit trades и Tardis
trades/L2, CLI plan/import, provenance/quality manifest и bounded sample-check в GitHub.
[Источники/границы](docs/ml/EXTERNAL_HISTORY.md). Обучение не выполнялось.

В M0 добавлены независимые immutable contracts, проверка версий/свежести прогноза,
чистая таблица приоритета и read-only status CLI. Модель НЕ обучена; worker,
feature exporter, inference и путь к ордерам НЕ реализованы. В engine/config/
profiles/dependencies изменений нет. Не регистрировать заглушку как стратегию.

Согласовано: общий scanner/рынок/капитал/риски/брокер, ML отдельно и без ожидания
обычных стратегий. Один владелец, готовый rule приоритетнее ML; наблюдение можно
заменить готовым ML-планом. Прогноз не истина и не основание двигать рыночные уровни.
Приоритет и функция выбора в M0 — спецификация, не уже атомарный dispatcher.

Дальше M1b: внешняя история входит в основу обучения, собственные capture проверяют
совместимость с ботом. На пригодных источниках нужны coverage/metadata, causal features,
фактический label/exit plan и purged splits. Current-12h сначала проверяется на
своих исходниках/зависимостях. Не обучаться только на сделках или hindsight winners.
Новые рыночные запуски/обучение рядом с активным ботом сейчас не назначены.

Проверки M0 и полной базы фиксируются в PR этой ветки. CLI:
`python -m scalp_bot.ml`; тесты: `python -m pytest tests/ml -q`.
При передаче работы следующему исполнителю читать MODEL_CARD и ROADMAP; не называть
следующий статус completed без реальных артефактов/измерений. Main не обновлять
в каталоге работающего capture даже после возможного слияния документации.

## Продолжение M1b1

Реализованы collector-time alignment Tardis imports и чистый версионированный
MarketContext→FeatureSnapshot с 49 полями/coverage masks. Документ:
[состояние M1b1](docs/ml/M1B_ALIGNMENT_FEATURES.md). Это не готовый dataset:
raw→context, historical metadata, labels и splits — M1b2; модель не обучалась.

Проверка current-12h зафиксирована отдельно в issue #55 и документационном PR #56.
ETH stop/reentry, scanner retention и общий промежуточный −34.37 USDT пока
не объяснены сырыми событиями. Не смешивать эту задачу с результатами ML-архивов.
