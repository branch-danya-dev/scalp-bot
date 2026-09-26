# P1: общий replay — архитектура и первый инкремент

25 сентября 2026. Реализован первый шаг: управляемое время в существующем движке и paper execution. Полный replay стратегии/портфеля пока не реализован. P0 recovery/fault stress остаётся открытым; начало инфраструктуры P1 не означает закрытие P0 или допуск E01.

## Что обнаружено в текущем replay

`scripts/replay_strategies.py` → `OfflineStrategyReplay.run_rows()` пересчитывает shadow-сигналы по research frames. Он создаёт стратегии с текущими defaults, использует local row timestamp вместо нового exchange-clock контракта, не воспроизводит clock admission, арбитраж, risk, broker, consumed setups и полный lifecycle. Загрузка JSONL выполняется целиком в память.

Этот инструмент пригоден только для ограниченного исследования сигналов. В CLI добавлено явное предупреждение; выдавать его сигналы за воспроизведённые сделки или PnL нельзя. Его алгоритм этим инкрементом не переделан в полный replay.

Текущие research frames сохраняются примерно раз в три секунды, orderbook snapshots — с ограниченной глубиной. Trade delta позволяет восстановить часть ленты, но не последовательность изменений fast/deep book. Нет полного порядка применённых сообщений, timer callbacks, coalesced evaluation и REST/bootstrap результатов. Из этих записей нельзя точно восстановить все субсекундные решения, отмены и fills. Старые файлы сохраняются как evidence; отсутствующие события не синтезируются под желаемый результат.

## Реализовано: единый источник времени

- `RuntimeClock` задаёт wall time, monotonic seconds и counter nanoseconds.
- `SystemRuntimeClock` сохраняет системные часы обычного запуска.
- `ReplayRuntimeClock` меняется только явными наблюдениями. Повторное чтение не продвигает время, sleep не нужен. Wall jumps разрешены; нарушение порядка monotonic или некорректное значение отвергается до изменения состояния.
- `TradingEngine(..., clock=...)` передаёт один объект брокеру, recorder и создаваемым ActiveSymbolSession. Чтения времени в engine/session/broker проходят через этот объект, включая время активации, cooldown, run elapsed, receipt age, pending/position age, partial/close и event timestamps.
- Создание Position/PendingEntry брокером явно задаёт monotonic timestamps. Legacy defaults прямого конструирования dataclass сохраняются для совместимости, но broker их не использует.
- `MarketClock` по-прежнему оценивает биржевые bounds независимо. Подстановка runtime clock не заменяет синхронизацию и не делает недостоверный рынок пригодным для входа.

Recorder timestamps и ISO теперь происходят из одного наблюдения. Имена файлов и реальные таймауты дисковых очередей остаются эксплуатационными, не виртуальными. Сетевые адаптеры и asyncio scheduler пока не переводились в offline режим; инъекция ReplayRuntimeClock **сама по себе не отключает сеть**. Offline driver должен вызывать обработчики без `engine.start()` и без live background loops либо использовать отдельные адаптеры.

## Проверка

Новые тесты выполняют настоящий PaperBroker и clock admission настоящего TradingEngine. Доступ к OS-clock adapter в проверяемых путях заменён на исключение. Проверяются:

- повторение одинаковой последовательности paper-входа, pending timeout и no-follow-through с идентичными событиями, комиссиями и балансом;
- скачки wall time вперёд и назад без преждевременного monotonic timeout;
- partial take и закрытие runner с заданными timestamps;
- истечение MarketClock, отмена pending через арбитр, восстановление по новой синхронизации; recovery не освежает старые receipt timestamps;
- совпадение виртуального времени в engine events и JSONL recorder;
- отклонение переставленных/нечисловых clock observations без частичной мутации состояния.

Это компонентная воспроизводимость исполнения и допуска, не live/offline parity сигналов или всей сессии. Торговые thresholds, комиссии и правила fills не менялись.

## Следующие инкременты

| ID | Работа | Критерий |
|---|---|---|
| P1-01 | Управляемое время engine/session/broker/recorder | Реализовано, тесты без ожидания реального времени |
| P1-02 | Контракт полного входного журнала и проверка покрытия | В работе: [v4](p1-input-journal-v4.md) добавляет periodic/UI dispatch, transport states и policy artifact; остаются нагрузочная проверка и подтверждение покрытия через offline dispatcher |
| P1-03 | Общий обработчик событий и offline adapters | В работе: [segments](p1-offline-segments.md), [fast scheduler](p1-offline-scheduler.md), [cold bootstrap](p1-offline-bootstrap.md), [clock/UI](p1-offline-clock-ui.md), [periodic](p1-offline-periodic.md), [scanner](p1-offline-scanner.md), [context](p1-offline-context.md), [controls](p1-offline-controls.md), [transport](p1-offline-transport.md), [service lifecycle](p1-offline-service.md), [source errors](p1-offline-source-errors.md), [context REST-await](p1-offline-context-await.md), [scanner/bootstrap REST-await](p1-offline-source-await.md), [clock REST-await](p1-offline-clock-await.md), [source diagnostics](p1-offline-source-diagnostics.md), [service/portfolio fixture](p1-offline-portfolio.md). Полный session dispatcher и checkpoint середины сессии ещё не реализованы |
| P1-04 | Автоматическое сравнение live/offline | Сопоставление кандидатов/отказов/intents/fills/partials/PnL с причинными ID; сохранённые различия, а не только итоговая сумма |

Контракт P1-02 должен описать отдельные exchange event time, оригинальное receipt monotonic time и processing observation, глобальный порядок применения, symbol activation/deactivation, полные book snapshot/delta (обоих каналов), trade/kline batches, REST context/clock responses, operator Start/Stop/toggles и callbacks evaluate/arbiter. При восстановлении важен исходный порядок; сортировка по биржевому timestamp может раскрыть события раньше получения. Сырые credentials/headers в журнал не входят. Это требования к следующему инкременту, не объявление уже реализованной схемы.

До готовности P1-02/03 новый длительный прогон для replay не запрашивается: полноту формата необходимо проверить общим offline dispatcher, а стоимость записи — нагрузочным тестом. E01 сравнивается только после прохождения parity, на одном полном потоке и независимых портфелях.
