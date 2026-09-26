# Внешняя история: M1a

Дата проверки источников: 26 сентября 2026. Внешние архивы — основной кандидат
для широкого обучения, а не дополнение, которое ждёт месяцы собственных прогонов.
Собственные capture остаются источником проверки online-признаков, scanner,
времени получения и исполнения. Один использованный для настройки период нельзя
одновременно называть независимым тестом.

## Что реализовано сейчас

`scalp_bot/ml/history/` — изолированный импорт **одного полного локального файла**
CSV/CSV.gz за заданные UTC-сутки. Никаких imports в engine, подписок, обучения,
ML-worker или изменения текущего capture. Зависимости только standard library.

| Source provider | Поддержанный формат | Что не следует из его наличия |
|---|---|---|
| `bybit-public-trades` | timestamp (decimal seconds), symbol, side, size, price, trdMatchID | Нет времени получения ботом/поставщиком и нет стакана |
| `tardis-trades` | bybit, timestamp/local_timestamp в microseconds, id, side, price, amount | Время получения относится Tardis, а не боту |
| `tardis-l2` | incremental_book_L2: snapshot + абсолютные количества уровней | Нет exchange sequence, disconnect records, L3-очереди или гарантии полного покрытия |

M1a принимает только отдельные Bybit USDT-linear symbols. Это заявленный тип
контракта, не проверка исторической спецификации инструмента: размер контракта,
количество в базовом активе, tick/lot и листинг нужно подтвердить в M1b. В импорте
amount сохраняется **как exchange-native**, notional/fees не вычисляются. Spot,
inverse, опционы и агрегированные файлы всех инструментов не смешиваются молча.
Дополнительные CSV-колонки допускаются; неизвестные обязательные схемы отвергаются.

## Нормализация и гарантии

- Цена/количество остаются decimal-строками; дробные секунды переводятся в целые
  microseconds без float-rounding. Порядок строк сохраняется, автоматической
  сортировки по exchange timestamp нет. Порядковый номер импорта — НЕ биржевая sequence.
- Tardis deltas группируются по `local_timestamp`, а не по exchange timestamp.
  Consecutive snapshot block применяется целиком с очисткой предыдущего состояния;
  промежуточная полусобранная книга не выдаётся как доступный snapshot.
- Количество заменяет уровень, ноль удаляет. Нет сложения абсолютных количеств,
  неявного обрезания глубины, починки crossed book или выдуманного maker-fill.
  До первого snapshot изменения сохраняются, но книга помечается uninitialized.
- EOF/CRC gzip, схема, сторона, положительные цены/объёмы, инструмент/сутки,
  порядок collector clock, ограничения памяти/размера проверяются. Подозрительные
  crossed/one-sided состояния и recent duplicate IDs видны в отчёте; исходники
  не исправляются. Conflict одного trade ID прекращает импорт.
- Manifest содержит SHA256 исходника и нормализованного файла, hash модулей
  адаптера, единицы/часы, диапазон времени, счётчики, limits и предупреждения.
  Повторный импорт даёт одинаковый hash events; время создания manifest может отличаться.
- Destination должен быть новым. При ошибке временные outputs удаляются; успешный
  manifest публикуется последним. Имеющиеся capture и raw не переписываются.

Даже `structural_checks_passed_only` **не означает готовый обучающий датасет**.
`training_ready=false`, `capture_replay_compatible=false`, `full_day_coverage_proven=false`.
Отсутствие непрерывной биржевой sequence/событий отключения нельзя исправить размером
выборки. Пауза в потоке сама по себе не доказывает gap. Recent duplicate проверяется
в окне 10000 IDs; полная межфайловая дедупликация ещё не реализована.

CSV.gz провайдера НЕ подаётся в `replay-paper-capture.py`: там иной контракт и hash chain.
Нормализованный событийный файл предназначен для будущего M1b adapter, не для
прямого запуска стратегий. Межпоточное объединение сделок/стакана, одинаковые
local timestamps в разных файлах и интервалы неизвестной ликвидности требуют
явной политики. События разных коллекционеров не объявляются общей точной лентой.

## Команды для отдельной рабочей копии

Сначала создать descriptor, не обращаясь к сети:

```powershell
python -m scalp_bot.ml.history plan --provider bybit-public-trades --symbol NEARUSDT --day 2024-01-01 --purpose development | Set-Content -Encoding utf8 source.json
```

После получения файла допустимым способом:

```powershell
python -m scalp_bot.ml.history import --source source.json --input data/ml/raw/NEARUSDT2024-01-01.csv.gz --output data/ml/normalized/near-20240101
```

Для Tardis использовать provider `tardis-trades` или `tardis-l2`. URLs строятся
из фиксированных шаблонов; **построенный URL не доказывает наличие данных**. Опциональный
`--expected-sha256` закрепляет уже проверенный источник. Распределение train/test
не определяется папками или текущей датой автоматически.

### Ограниченная проверка настоящих архивов

`python -m scalp_bot.ml.history.samples --output <новая папка>` — отдельная явная
сетевая команда. Она скачивает ровно NEARUSDT за 2024-01-01 из трёх источников выше,
без API-ключей, оплаты, proxy-обходов и подписок. Максимум 128 МиБ на файл, временной
бюджет; redirect/ошибка/превышение прекращают конкретную проверку и отмечаются в отчёте.
Усечённый файл не считается успешным полным импортом. Выбор даты — format smoke,
не оптимизация прибыли; эта выборка не является holdout или обучающей историей.

Веточный workflow `ml-history-samples.yml` запускает только эту offline-историю,
не рыночную торговлю. Он сохраняет **report.json с метаданными, не vendor datasets**.
Обычный pytest не использует сеть. Не выполнять sample/download/import в каталоге
идущего бота: для текущей работы используются GitHub/изолированная рабочая среда.

## План данных до обучения

**M1a (сейчас):** источники, изолированные readers, source/hash manifests и проверки форматов.

**M1b (следом):** historical instrument metadata и coverage/incident inventory;
фиксированный universe/интервалы ex ante; сопоставление потоков и gaps;
общий причинный feature adapter; plan/label policy; global purged time splits;
проверка offline/online parity. Затем M2 — обучение baseline и CatBoost.

У обучения две явно раздельные возможности: (1) candles/trades-only baseline,
(2) модель с согласованным L2. Первую нельзя выдавать за умеющую читать плотности.
Для второй используем только пересечение покрытых периодов/признаков. Missing
не заменяется фиктивным нулём и не заполняется будущими значениями.

Внешняя training-universe может быть шире реально записанного scanner. Она должна
быть зафиксирована заранее с учётом listing/delisting, а не состоять только из
сегодняшних выживших или исторических победителей. Полный тест гибрида использует
восстановленный scanner membership либо честно объявленный fixed universe; эти
результаты не смешиваются с exact replay собственного capture.

Не скачивать месяцы/террабайты и не приобретать платные архивы до проверки образцов,
покрытия, прав использования и места. Большая история даёт разнообразие режимов,
а не автоматическую прибыль. В main и текущей 12h-сессии изменений нет.

## Проверенные публичные источники

- Bybit public directory: https://public.bybit.com/trading/
- NEAR archive listing: https://public.bybit.com/trading/NEARUSDT/
- Tardis schemas: https://docs.tardis.dev/downloadable-csv-files/data-types
- Tardis dates/download/access: https://docs.tardis.dev/downloadable-csv-files/api
- Bybit channel/date coverage: https://docs.tardis.dev/historical-data-details/bybit
- Reconstruction: https://docs.tardis.dev/faq/order-books
- Clocks/gaps/native quantities: https://docs.tardis.dev/faq/data

Не считать каждую эпоху исторического L2 идентичной текущим L50/L1000 бота:
покрытие и каналы менялись. Provider CSV не содержит всех native channel IDs/u/seq.
Доступ без API-ключа к первым дням месяца у Tardis — правило доступа, не лицензия
на публикацию. В репозиторий входят только код, синтетические тесты и компактные
provenance/validation reports; raw, labels, datasets и weights остаются вне Git.
