# P1-03: scanner, активация инструментов и REST-контекст

Следующий инкремент: [периодический context loop](p1-offline-context.md). Source errors и REST-await interleaving остаются открытыми.

`_scan_once` получает ranked candidates из REST и передаёт их общему `_apply_scanner_result`. Live и offline выполняют одинаковое обновление candidates, mark/funding metadata, last_ranked_at, promotion, capacity eviction, idle cleanup и scanner_update. Порядок candidates сохраняется; сортировка при replay не выполняется.

Оба offline adapter поддерживают scope scan. OfflineScheduledReplay также запускает исходный `_scanner_loop`, включая выбор интервала для пустого/непустого universe, wait/wake/cancellation. Вложенный bootstrap восстанавливается из записанного полного результата и применяется исходным `_apply_bootstrap_result`. Отсутствующий либо относящийся к другому символу bootstrap вызывает отказ.

## Граница worker lifecycle

Запуск WebSocket worker выделен в `_launch_symbol_worker`. Live по-прежнему создаёт обычную asyncio Task. Offline не создаёт сетевой worker; сбрасывает локальный market handler для нового worker lifetime. В этом инкременте проверяются синхронные изменения scanner/session state, а не transport transitions.

Transport events по-прежнему неподдержаны и не пропускаются. Поэтому наличие scanner adapter не означает, что реальная сессия с подключением сокетов уже воспроизводится целиком. Ошибки bootstrap/scanner, поднятые scopes и перемежение событий во время REST await также пока отклоняются. В частности, source_error хранит тип, тогда как старый scanner_error output содержит текст исключения; нельзя выдумывать недостающий текст для совпадения outputs.

## REST-контекст

Используется уже общий `_apply_context_result`: полные записанные свечи восстанавливаются до мутации, затем применяется исходная reconciliation логика и сброс cache key. Проверен context refresh между двумя scanner rotations. Периодический context_loop с его параллельными REST запросами этим инкрементом не поддержан.

## Проверки

Полный набор: **660 passed**, 60.77 секунды в чистом окружении. Добавлены шесть тестов scanner/rotation/context.

Cold engine по настоящему capture prefix повторяет AAA → BBB → AAA при capacity=1. Проверяются исходный ranking, promotion/deactivation, повторная активация, mark price, свечи и все output/engine events. Сценарии выполняются segment adapter, scheduled adapter и periodic scanner loop. Live fixture изолирует transport через worker launch adapter; остальные scanner/cleanup/bootstrap/context обработчики настоящие.

В replay запрещены asyncio.create_task/sleep. Корректно перехешированные изменения ranking, отсутствие bootstrap и неверный symbol приводят к отказу. Настоящие REST и WebSocket не используются.

Следующий этап — context loop, paper timer/operator controls, service lifecycle, transport и REST-await interleaving. Полная session parity и load validation ещё не выполнены; parityReady=false. Новый пользовательский прогон пока не нужен. Торговые правила и настройки не менялись, сервер не перезапускался.
