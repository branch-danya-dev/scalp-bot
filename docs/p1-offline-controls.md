# P1-03: operator controls и paper timer

Следующий инкремент: [transport/worker replay](p1-offline-transport.md). Полный service lifecycle остаётся открытым.

OfflineScheduledReplay поддерживает успешные start_request, toggle_strategy, stop и paper_timer. Он вызывает исходные TradingEngine.set_running/_stop_trading/toggle_strategy; при холодном offline engine прямой вызов set_running по-прежнему запрещён вне dispatcher. Segment adapter control-вызовы не поддерживает.

## Run manifest

Создание manifest и запуск таймера выделены в `_build_trading_manifest` и `_launch_run_timer`; live defaults сохраняют прежнее поведение. Offline Start берёт manifest из записи, проверяет его внутренние hashes, manifest/schema/execution model версии, config/source/runtime linkage, policy и текущий enabled set. UUID заново не генерируется, source/runtime probes внутри replay не вызываются. Start требует provenance-bound cold engine.

После проверенного control окна origin обновляет fingerprint enabled strategies. Это позволяет продолжать после записанного toggle; произвольная смена набора между окнами по-прежнему отклоняется admission проверкой.

## Таймер

Start создаёт offline coroutine с тем же причинным ContextVar context. Dispatcher исполняет её только по paper_timer scope, затем по wait/wake/cancelled. Исходный `_paper_run_timer` вызывает Stop с duration_elapsed; ручной Stop запрашивает cancellation. Отмена до первого исполнения закрывает coroutine без синтетических scope markers, как live asyncio Task. Cancellation спящего paper timer требует предшествующего control request.

Перекрывающиеся незавершённые paper timers пока отклоняются. Service close и отказ Start по readiness не объявляются поддержанными сценариями; полного service/session dispatcher ещё нет.

## Сравнение outputs

Сохраняются все реальные output events. Для сравнения scheduled replay исключает только `run_summary.latencyMetrics` и `run_summary.recorderHealth`: они описывают глобальную telemetry и инфраструктуру записи, а не торговый результат. Список исключений явно возвращается в `outputComparisonExclusions`. Исключения не распространяются на другие event types.

Все остальные поля, в том числе balance, realizedPnl, число сделок, причины, timestamps, elapsed time, manifest и его IDs, сравниваются без подстановки ожидаемых значений. Подмена PnL или причины Stop вызывает отказ. `outputsMatch=true` означает совпадение с указанными исключениями; `parityReady` остаётся false.

## Проверки и остаток

Полный набор: **672 passed**, 66.82 секунды в чистом окружении. Добавлены семь тестов controls/timer и правил сравнения.

Тесты используют одинаковый заранее разрешённый admission и проверяют toggle → Start → toggle → manual Stop либо duration_elapsed, включая отмену до первого запуска таймера. Внутри replay запрещены asyncio.create_task/sleep. Совпадают manifest, strategy set, итоговые торговые поля и output events с явными исключениями. Fixture без позиций не является доказательством повторения полного портфельного закрытия или рыночной готовности к Start.

Негативные проверки изменяют enabled set run manifest, realizedPnl и reason. Engine после исполнения с mismatch получает failed state; overrides восстанавливаются.

Остаются service/transport lifecycle, source errors, REST-await interleaving и обработка незавершённых coroutines между окнами. Полный JSONL и session parity ещё не готовы; новый пользовательский прогон пока не нужен. Торговые настройки не менялись, сервер не перезапускался.
