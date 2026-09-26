# Model card — trend_impulse_ml

Статус: **NOT_TRAINED / NOT_CONNECTED / NO_ORDER_AUTHORITY**.
Результат этой ветки — M0, не модель, выдающая рыночные прогнозы.

| Поле | Текущее значение |
|---|---|
| Назначение | Исследование импульсного продолжения на отобранных scanner инструментах |
| Кандидат | CatBoost; logistic и простой немодельный impulse baseline для сравнения |
| Обученные веса / hash / model_version | Нет |
| Feature schema / label policy | Контракт задан, фактический adapter/пороговый план M1 не реализованы |
| Train/calibration/validation/test периоды | Не выбраны |
| Доказанная достаточность данных | Не установлена |
| Метрики прогноза / calibration | Не измерены |
| Portfolio delta-net / drawdown / costs | Не измерены |
| Inference p50/p95/p99 / hardware | Не измерены |
| Runtime mode | Не подключён, не зарегистрирован в стратегиях |
| Допуск | Только разработка контрактов и offline-тесты; не paper/live торговля |

Перед M3 заполнить source commits/raw hashes, labels/execution/fees, splits,
feature order/units/null policy, seeds/dependency lock, calibration method,
artifact SHA, hardware/threads и измеренные ограничения. Перед M4 добавить
проверенный plan policy, simulation provenance и явное решение допуска.

Вероятности не являются фактами рынка. Результаты на NASDAQ/чужих моделях не
переносятся на эту карточку. Исторические выигрыши обычного бота не приписываются ML.
