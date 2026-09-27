# Данные и критерии полезности

## Источники и причинность

Основа широкого обучения — пригодные внешние архивы Bybit плюс собственные capture.
Список источников, реализованный импорт M1a и ограничения: [EXTERNAL_HISTORY.md](EXTERNAL_HISTORY.md).
Для проверки точного поведения бота нужны его входной журнал, bootstrap и membership
в scanner на каждом событии; внешний архив не подменяет эту запись. Временная последовательность — исходный порядок получения
и применения, а не сортировка будущего полного рынка только по exchange timestamp.
Не добавлять задним числом монеты, которых бот тогда не наблюдал.

`verify_capture` сначала проверяет исходный baseline с его source/dependencies.
Экспортер ML затем идёт потоком/через IndexedInputs, не загружает многогигабайтный
gzip в list. Исторический baseline replay и новая counterfactual симуляция различаются:
новая стратегия меняет расписание/сделки/капитал. Ей нужен market-only адаптер с
виртуальными часами и очередями. Старые решения не принуждают новый портфель
повторять старые входы, а старый recorder не ослабляется ради parity.

Текущий E01Comparison не универсален: только E01/E06, explicit callbacks,
event-driven=False. Его разделение бюджетов можно переиспользовать, но совместимость
с ML/current-12h требуется реализовать и проверить, не просто заявить.

## Feature schema v1 — план адаптера

| Семейство | Источник | Кандидаты признаков |
|---|---|---|
| Краткое движение | FormingCandleContext | microMove5s/15sBps, microRange, velocityBpsPerSecond |
| Свеча | pre_state + closed history | body/wicks/closePosition, pace/expansion, возраст snapshot |
| Поток | MultiHorizonFlowContext | imbalance, CVD, normalized OFI, count/notional 5/15/60s |
| Стакан | fast/deep + execution health | spread bps, доступная глубина, дисбаланс, coverage/age |
| Плотность | LiquidityEvidence | remaining, attack, depletion, replenishment, wallPresent/unknown |
| Структура | MarketStructure / LocalRegime | расстояния до препятствий, наблюдаемый режим, диапазон/эффективность |

Это перечень для M1, не готовый extractor. Фактические единицы/окна/хеш схемы
фиксируются после проверки полей. Реализация upstream может заполнять некоторые
нулевые значения: ML-адаптер должен учитывать count/coverage и не выдавать их за
действительно наблюдаемый нулевой поток. Общие признаки не рассчитывать второй раз
в worker. Snapshot не должен хранить mutable-ссылки на engine state.

## Выбор наблюдений и labels

Брать не только trade_opened, но все причинно доступные моменты выбранного
sampling rule: импульсы, ложные ускорения, спокойные окна и отсутствие старого сигнала.
Дедупликация по рыночному эпизоду, вес/negative subsampling фиксируются до обучения.
Не обучать на «лучших упущенных сделках» и максимумах hindsight-review.

Первая задача: вероятность target-first / stop-first / timeout для long и short
при **заранее определённом** ImpulsePlanPolicyV1. Горизонт 30s — предложение для
первого эксперимента; 15/60s — диагностические горизонты, не молчаливый поиск лучшего.
Численные границы target/stop/TTL/sample cadence пока не утверждены. До разметки M1
их нужно записать в experiment manifest, основываясь на тогда наблюдаемом диапазоне,
структуре и экономике. Цель не выводится из желаемого R:R или будущего максимума.

Учитывать arrival latency, исполнимую сторону/глубину, комиссии, slippage, funding,
реальное правило partial/target из фиксированного execution profile. Касание maker
уровня не равно fill. Не использовать другую «дешёвую» модель расходов для ML.
План с недостаточной наблюдаемой глубиной не объявляется доказанно исполнимым.
Пробел записи/цензурированный конец горизонта — invalid/unknown, не timeout/проигрыш.
Одновременные OHLC-касания без порядка не трактовать в пользу стратегии.

Probability up != expectancy. В M2 дополнительно оценивать conditional payoff
каждого исхода, включая ненулевой net при timeout и частичные exits. Три вероятности
не дают сами по себе денежное ожидание. Финальная оценка — полный simulated ledger
общим брокером, не сумма удачных labels/markout.

## Разделение данных

Глобальные временные блоки одновременно по всем монетам; не перемешивать соседние
события train/test. Отдельные train, calibration, validation для выбора thresholds,
и нетронутый test. Удалять train-label интервалы, пересекающие начало validation/test;
учитывать самый длинный label horizon и latency, а не только произвольное число строк.
Один market episode не делить между наборами. Scalers/imputation fit только на train;
калибратор не подгонять по holdout. Соседние тики и коррелирующие монеты не считать
независимыми наблюдениями. Веса/пороги/варианты и seeds регистрируются.

## Baselines и принятие

Сравнить: постоянная/no-trade модель, простой немодельный impulse rule,
обычный бот и гибрид rule+ML. Начальный обучаемый кандидат — небольшой CatBoost,
логистическая модель — контроль сложности. Neural/ONNX не требуются в v1.
Зависимости добавляются отдельным optional extra/lock только на этапе M2.

ML-метрики: calibration/Brier/log loss, class balance, coverage/abstention и
стабильность по времени/инструментам. Торговые: net, fees/turnover, expectancy/R,
drawdown/tails, fill rate, доля устаревших прогнозов, вытесненные rule-сделки,
концентрация результата и загрузка общего риска. Сравнение портфелей — одинаковый
поток и стартовый бюджет, но независимые балансы baseline/candidate. Внутри гибрида
rule и ML делят ОДИН бюджет.

Перед test записать минимально полезный delta-net и риск/latency limits. Не назначать
магические 30 сделок, accuracy=70% или +10% за день доказательством. Для торгового
продвижения требовать положительный вклад после расходов, не сводящийся к одному
эпизоду, устойчивость к ухудшению исполнения, отсутствие регрессии ограничений;
оценивать неопределённость по временным блокам. «Недостаточно данных» допустимо;
продлевать исследование до первого плюса запрещено. M0 не знает мощности будущей выборки.

## Публичные источники: обоснование, не результаты этого бота

Проверены 26 сентября 2026:
- CatBoost predict_proba: https://catboost.ai/docs/en/concepts/python-reference_catboostclassifier_predict_proba
  Поддерживает вероятности классов и thread_count; runtime benchmark ещё нужен.
- scikit-learn TimeSeriesSplit: https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
  Временной порядок/gap полезны, но purging по интервалам labels нужен отдельно.
- Калибровка: https://scikit-learn.org/stable/modules/calibration.html
  Score не считается достоверной вероятностью без проверки.
- ONNX threading: https://onnxruntime.ai/docs/performance/tune-performance/threading.html
  Число потоков/CPU contention важны; ONNX — возможная поздняя оптимизация, не M0-зависимость.
- Briola et al., Deep Limit Order Book Forecasting: https://arxiv.org/abs/2403.09267
  Исследование NASDAQ подчёркивает различие forecast metrics и торговой полезности;
  не доказывает edge на Bybit и не предоставляет готовую обученную модель для нас.
