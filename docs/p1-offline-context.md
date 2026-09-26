# P1-03: периодическое обновление REST-контекста

Следующий инкремент: [operator controls/paper timer](p1-offline-controls.md). Service/transport и REST-await interleaving остаются открытыми.

OfflineScheduledReplay поддерживает исходный `_context_loop`: ожидание 60 секунд по журналу, snapshot списка sessions, проверка свежести минутных свечей, применение результатов в порядке инструментов, следующее ожидание и cancellation.

Сетевое получение вынесено в `_fetch_context_results`. Live по-прежнему использует вложенные asyncio.gather и return_exceptions=True; порядок применения результатов не менялся. Общий `_context_needs_1m` определяет, нужен ли минутный REST refresh. Offline вызывает ту же проверку на записанных clock observations, затем восстанавливает полные свечи из будущих записей этого batch; сами записи потребляет общий `_apply_context_result` в исходном порядке.

## Контракт offline batch

Количество и порядок rest_context результатов должны соответствовать snapshot items. `candles=null` обязано совпадать с веткой «минутная история свежая»; список, включая пустой, — с веткой REST fetch. Higher-timeframe свечи восстанавливаются вместе с исходным confirmed. Пропуск результата, другой symbol или другая freshness branch вызывает отказ.

Предварительный просмотр batch не применяет свечи раньше времени: мутация остаётся внутри исходного context handler и сверяется строгим cursor. При interleaving другого события во время REST await текущий cursor не совпадёт и replay откажется. Поддержка асинхронных REST completions ещё не реализована.

Source errors, включая частичный сбой одного инструмента, пока не поддержаны. Они не заменяются пустыми свечами и не пропускаются. Окно должно закончиться после завершения/отмены loops; продолжение при ожидающей coroutine не разрешено.

## Проверки

Полный набор: **665 passed**, 65.39 секунды в чистом окружении. Добавлены пять тестов context loop.

Cold engine восстанавливает два инструмента: у AAA устаревшая минутная история, у BBB свежая. Live fixture завершает запросы AAA позже BBB, но общий loop применяет batch в порядке AAA/BBB. Offline повторяет свечи, HTF context, cache invalidation и все output/engine events без asyncio.sleep/create_task/gather.

Отдельно проверены пустой список sessions и cancellation. Негативные тесты с корректной hash chain изменяют freshness branch, порядок символов и наличие результата; replay отказывает и помечает engine failed. Настройки стратегии, интервалы и thresholds не менялись.

## Остаток P1

Операторские Start/Stop/toggle, paper timer, service lifecycle, transport/worker transitions, source errors и REST-await interleaving остаются открытыми. Полный session replay и сравнение fills/partials/PnL ещё не готовы, parityReady=false. Новый длительный пользовательский прогон пока не нужен; сервер не перезапускался.
