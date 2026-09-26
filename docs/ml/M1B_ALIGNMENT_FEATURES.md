# M1b1: согласование архивов и общий адаптер признаков

Следующий инкремент после `b0874d8`. Это часть M1b, **не завершённый обучающий
датасет**. Main/current-12h и локальная копия пользователя не меняются.

## Реализовано

`ml/history/alignment.py` принимает два законченных импорта M1a: Tardis trades и
Tardis L2 одного инструмента, дня, рынка и purpose. Проверяет checksum, manifest,
счётчики, непрерывность локальной нумерации импорта и порядок collector timestamps.
Это НЕ проверка биржевого sequence: его в этих нормализованных CSV нет.

Потоки объединяются по `available_time_us`, не по биржевому времени. Одинаковое
время между каналами образует одну группу наблюдения. Внутри каждого канала порядок
сохранён; взаимный порядок разных каналов не объявляется известным. Потребитель
может построить признаки после группы, но не симулировать точный fill в её середине.
Полный snapshot применяется атомарно. Обновления до первого snapshot остаются
`uninitialized`. Ограничены строки, группа, объём чтения/записи; перезапись запрещена.
Ошибка не публикует завершённый output. Исходники импорта и raw не меняются.

Результаты: `aligned.jsonl.gz` и `alignment-report.json`, последний публикуется
после полной проверки. Отчёт сохраняет SHA исходников/manifest/нормализованных
событий и кода, исходные quality warnings, observed overlap, состояния книги,
количество неоднозначных совпадений времени и паузы потоков.

**Observed overlap не доказывает отсутствие пропусков.** Пять секунд без обновления
по умолчанию — лишь диагностический порог, не торговая настройка и не доказательство
разрыва соединения. Нет receive time нашего бота, scanner membership и точного
historical fill ordering. Bybit public trades без collector clock для этой склейки
не принимаются. Training/replay/full-day-coverage flags остаются false.

## Признаки

`ml/features.py` реализует один чистый `extract_context_features()` от настоящего
`MarketContext` к существующему immutable `FeatureSnapshot`. Отдельные scanner,
индикаторы и алгоритмы плотности не создаются. Порядок, единицы и missing semantics
49 числовых полей имеют устойчивый SHA-схемы. Машиночитаемая версия:
`research/ml/market-context-v1.json`.

Поля: исполнимость/возраст книги, spread bps, top-5 depth; covered flags и
trade count/notional/CVD/imbalance/OFI 5/15/60s; body/range/pace/micro movement
формирующейся свечи; состояние наблюдаемой стены; расстояния до уровней.
Позиции, PnL, будущие outcomes, желаемые target и статистика winners не являются
входами. Модельный прогноз по-прежнему не создаёт рыночные уровни.

`ContextCoverage` явно сообщает, какие окна trades/OFI и остальные группы
производитель действительно восстановил. По умолчанию неизвестные признаки None,
а не ноль. Полное наблюдаемое окно без trades даёт count=0; отсутствие окна — None.
Monetary/normalized-depth поля требуют явного подтверждения единиц. Same-time
flow/forming, symbol, schema и observation time проверяются; future snapshots
отвергаются. Старое содержимое flow не освежается новой датой MarketContext.

Пример без модели и ордеров:

```python
from scalp_bot.ml.features import FEATURE_SCHEMA, ContextCoverage, extract_context_features
# ref.feature_schema = FEATURE_SCHEMA; ref.market_time_ms = context.observed_at_ms.
# Не выставлять True без доказанной provenance/coverage конкретного источника.
snapshot = extract_context_features(context, ref, ContextCoverage())
```

Это проверка общего адаптера на настоящих классах. **Полный путь raw→MarketContext→
features, историческая metadata и end-to-end offline/online parity ещё не реализованы.**
Наличие функции и корректного schema hash не доказывает причинность её производителя.

## Команды в изолированной копии

```powershell
python -m scalp_bot.ml.history align --trades data/ml/tardis-trades --book data/ml/tardis-l2 --output data/ml/aligned
python -m pytest tests/ml/test_alignment.py tests/ml/test_features.py -q
```

Команда align работает с локальными M1a imports, без сети. Публичный sample workflow
теперь отдельно выполняет также align NEARUSDT 2024-01-01 и сохраняет только
metadata report. Форматный день не является обучающей выборкой или holdout.
Результат настоящего workflow фиксируется в PR после выполнения, не заранее.

## Следующий ограниченный этап M1b2

Подтвердить historical instrument units и universe; собрать причинный adapter
упорядоченных внешних групп/своего capture к существующим структурам контекста;
доказать feature parity и покрытие окон. Затем заморозить label/exit policy,
sampling и purged temporal splits, реализовать потоковый feature/label exporter.
Только после этого M2: обучение и сравнение с простыми правилами. Нет покупок данных,
обученных весов, worker, подключения к риску/ордерам и нового рыночного запуска.

## Основание для времени/стакана

Официальные схемы Tardis: https://docs.tardis.dev/downloadable-csv-files/data-types
и правила восстановления: https://docs.tardis.dev/faq/data . Проверены при реализации.
В них local_timestamp — время получения провайдером, amount — абсолютное количество,
а согласованное состояние книги читается после полного пакета. Наше группирование
межканальных совпадений — консервативное правило наблюдения, не выданный провайдером
глобальный порядок исполнения. Сторонние данные и результаты не доказывают edge бота.
