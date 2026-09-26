# P1-03: service startup, shutdown и footer

Следующий инкремент: [partial context failure/book gap](p1-offline-source-errors.md). Остальные ошибки и REST-await interleaving остаются открытыми.

OfflineScheduledReplay поддерживает service start/close вместе с исходными engine.start/close. Live создание фоновых задач и их отмена/gather вынесены в `_launch_service_tasks`/`_shutdown_service_tasks`; defaults сохраняют прежнее поведение. Offline переопределяет только инфраструктурные адаптеры, не создаёт сетевые задачи, recorder writer или реальные ожидания.

## Жизненный цикл

Start принимается только для cold provenance-bound engine. Исходный start выполняет clock sync и scanner через существующие recorded-result adapters, затем объявляет scanner/context/arbiter loops и clock loop при включённых exchange clocks. Их фактический старт происходит по scope rows; неожиданный/повторный loop отклоняется.

Close выполняет исходную отмену pending, закрытие позиций/Stop при running, запрос отмены paper timer и `_stop.set()`. На инфраструктурном shutdown coroutine приостанавливается. Dispatcher исполняет записанные cancellations и transport cleanup; footer допускается только при отсутствии незавершённых fast/periodic задач и недренированных transport channels.

После cleanup исходный close завершает REST adapter (без сети), закрывает input journal и recorder. Cursor проверяет исходный footer inputCount. Остаток после footer, закрытие без запуска, повторный Start, ранний/отсутствующий footer отвергаются. Успешный engine получает origin state=closed и не принимает новые окна. Отчёт содержит serviceLifecycleMatched, но parityReady остаётся false.

Отмена фоновых циклов до их первого исполнения не создаёт искусственных scopes. Startup и shutdown должны находиться в одном поддержанном окне: перенос долгоживущих coroutines и service state между окнами ещё не реализован. Footer не доказывает отсутствие потерь ingress до записи; применяется существующий hash/sequence контракт.

## Сквозной тест

Полный набор: **687 passed**, 77.08 секунды в чистом окружении. Добавлены восемь тестов service lifecycle.

Записанный synthetic live lifecycle начинается с clock sync/scanner, создаёт один worker, запускает фоновые циклы и завершает их вместе с transport cancelled/drained. Offline восстанавливается по capture prefix и повторяет весь участок до footer. Проверены exchange clock on/off, немедленное закрытие до старта циклов и shutdown с активным paper timer. В replay запрещены asyncio.sleep/gather/create_task.

Совпадают output events, candidates, engine events и MarketClock state. Для run_summary сохраняются ранее объявленные исключения latencyMetrics/recorderHealth. Paper admission в timer-сценарии задан fixture, позиций нет; портфельное закрытие и готовность реального рынка этот тест не доказывает.

Негативные тесты с пересчитанными hashes проверяют обрезанный журнал, ранний footer, отсутствие start и повторный start. Ошибка оставляет engine failed; временные overrides восстанавливаются.

## До пользовательского прогона

Остаются source/market errors, rejected controls, перемежение событий во время REST await, атрибуция перекрывающихся workers и потоковое воспроизведение длинной записи. Затем необходимы capture load validation и полное сравнение торговых решений/fills/partials/PnL. Сквозной fixture не является допуском к production. Новый длительный прогон пока не нужен; торговые правила/профиль не менялись, сервер не перезапускался.
