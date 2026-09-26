# P1-03: периодические clock/arbiter loops

Следующий инкремент добавляет [scanner loop](p1-offline-scanner.md). Context loop и service/control/transport остаются открытыми.

OfflineScheduledReplay исполняет исходные `_clock_loop` и `_arbiter_loop` как приостанавливаемые coroutines. Live `_input_sleep` сохраняет прежние dispatch wait/wake/cancelled и использует новый `_periodic_sleep`, который по умолчанию вызывает обычный asyncio.sleep. Offline подставляет контролируемую паузу; задержки по реальным часам и asyncio Tasks не создаются.

Для каждой periodic coroutine сохраняются отдельный ContextVar context, состояние и ID текущего wait. Записи wake/cancelled выбирают ожидающую задачу, после чего тот же engine сверяет источник, ID и delay через cursor. Работа нескольких loops может перемежаться с market/UI/fast-task обработкой. Повторный запуск того же loop в одном окне и незавершённый wait на границе окна отклоняются.

Внутри clock loop вызов синхронизации получает записанный sample/error и применяет общий обработчик движка. Сеть не используется. Этот путь пока предполагает готовый результат синхронизации без перемежения других событий внутри ожидания REST; неизвестная последовательность приводит к отказу.

## Что проверено

Полный набор: **654 passed**, 57.74 секунды в чистом окружении. Добавлены пять тестов periodic dispatch.

Тест одновременно запускает live clock и arbiter coroutines с управляемыми пробуждениями, записывает UI между ними и сравнивает с offline исполнением. Clock проходит интервалы 2 → 4 → 2 → 4 секунды: холодный retry, успешная синхронизация, ошибка REST, восстановление. Арбитр реально вызывается; оба цикла отменяются в ожидании. Совпадают output events, engine events и MarketClock state. В replay запрещены asyncio.sleep/create_task.

Проверки с корректной hash chain отклоняют неверные wait ID, delay, source и обрезанное окно с ожидающими задачами. После отказа bindings восстанавливаются, coroutines закрываются в своих контекстах, экземпляр становится непригоден к продолжению.

Fixture задаёт одинаковое running state явно; операторский Start и торговые входы этим тестом не воспроизводятся. Это проверка dispatch и порядка исполнения, не полная session parity и не подтверждение эффективности стратегии.

## Что остаётся

Поддержаны только clock_loop и arbiter_loop. Scanner/context loops, paper timer, service Start/close, operator control, transport lifecycle и REST-await interleaving требуют следующих адаптеров. Между окнами пока нельзя сохранять незавершённые coroutines. Полный JSONL не воспроизводится, parityReady=false.

Торговые правила, retry/TTL параметры и профиль не менялись. Новый длительный пользовательский прогон пока не нужен; сервер не перезапускался.
