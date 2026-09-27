# Архитектура ML и параллельных сценариев

Текущий контракт обычных стратегий — [parallel-scenarios v3](../architecture/parallel-scenarios.md).
Один scanner и набор общих market facts; независимая подготовка на (symbol,strategy);
одна общая атомарная точка риска/резерва и один владелец исполнения. Старое
single-owner-before-signal заменено. Confidence стратегий не складываются.

## Реализованный offline путь

Raw собственных inputs в порядке processingMonoNs/source_sequence → CaptureContext
с общей OrderBookState, kline/trade overlay/OFI/flow/forming реализацией → прежний
49-полевой FeatureSnapshot → фиксированный plan/labels → временные выборки →
CatBoost/logistic/calibration → сохранённый predictor.

Bootstrap содержит историческую спецификацию, не текущий REST. Активация берётся
из собственного membership; fast gap/reconnect меняет epoch и сбрасывает прогрев.
Требуется новый snapshot и 60 с покрытого контекста. Недоступные structural/liquidity
группы остаются None с coverage=0. Price/stop/target/PnL labels не входят в features;
snapshot_quote сохраняется до будущего исполнения отдельно от label entry.

## Реализованный shadow путь

Immutable tuple FeatureSnapshot → InferenceWorker.submit (без ожидания) → один
spawn-процесс → versioned ImpulseForecast → poll → ShadowAdapter trend_impulse_ml.
Inbox/outbox по одному сообщению, latest mailbox не более 12 символов. Можно
объединить задания прогнозов, нельзя выбрасывать raw deltas. Модель thread_count=1;
startup 30 с, source TTL 1 с, prediction timeout 2 с. Зависший child завершается,
ошибка не становится прогнозом. Shutdown явно join/terminate, без живого child.

Forecast содержит capture/sequence/source time/clock domain/activation epoch,
feature/model/plan versions, produced/expiry. Источник не омолаживается окончанием
predict. Исторический harness явно отображает source time в локальный replay clock
и сохраняет исходный ref. Это не измерение исторической сетевой задержки.
ShadowAdapter проверяет metadata/freshness/повтор, формирует свой stop/target по
ImpulsePlanPolicyV1.1, не проходит через условия breakout. Ему не выдаются broker,
REST, scanner, капитал или право менять market facts. Child не импортирует Bybit.

В production engine ML hooks не включены. scripts/check-ml-shadow-offline.py
подключает наблюдатель только к замоканному локальному fixture. Будущий допуск
модельного плана потребует общего risk/executor и детерминированной защиты позиции;
при пригодном готовом ordinary-плане ему приоритет, без ожидания модели. Чистая
ml/arbitration.py сохраняет эту спецификацию, но не является торговым подключением.

## Что измерено

Архивный harness измеряет доставку готовых features→worker→adapter; synthetic
движок дополнительно включает общую оценку/формирование контекста и poll. Внешнее
получение пакета/парсинг сети этим не доказано. Бюджеты заданы до замера в
experiments/worker-budgets.json, среда в hardware.json. Основной event loop превысил
20 мс и с выключенным ML, и с shadow; полный M3 performance gate остаётся открытым.
