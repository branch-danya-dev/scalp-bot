# P1-03: исполнение отдельных участков общего движка

Следующий инкремент: [offline fast scheduler](p1-offline-scheduler.md) поддерживает задачи и interleaving между обработчиками. Ограничения отдельного OfflineSegmentReplay ниже сохранены.

[Cold bootstrap](p1-offline-bootstrap.md) добавляет восстановление config/policy и provenance admission. Для такого engine продолжение между сегментами обязано быть непрерывным; описанный ниже режим с вручную подготовленным engine остаётся компонентным API.

25 сентября 2026. Добавлен `OfflineSegmentReplay` в `scalp_bot/offline_segment.py`. Это первый исполняемый offline adapter, а не replay всей сессии. Он получает явно подготовленный idle TradingEngine и последовательность envelope-записей одного непрерывного scope из input journal v4.

## Общий путь live/offline

`TradingEngine._market_handler(symbol)` создаёт обработчик с fast/deep OrderBookState одного worker. WebSocket worker и offline adapter вызывают этот же обработчик. Применение snapshot/delta, обработка trade batch, защитные исполнения, fallback evaluation и запись кадров не дублируются и не меняют торговые правила.

Поддержанные корневые scopes: bootstrap_apply, rest_context_apply, market_message, evaluate, arbiter. Вложенные scopes выполняются самим движком, не запускаются повторно по строкам журнала. Один экземпляр adapter хранит обработчики между сообщениями одного worker lifetime.

## Строгая проверка исполнения

Перед вызовом проверяются schema/body, локальная hash chain и непрерывность sequence, структура вложенных scopes, завершение returned и допустимые виды событий. Повторное применение уже потреблённого scope и движение назад отвергаются. Между отдельными сегментами допускаются пропуски: это компонентный API, который не подтверждает полноту сессии.

Replay cursor сверяет каждый реально вызванный scope, input marker и clock read с очередной записью. Порядок, имена методов часов, scope IDs, символы и body должны совпасть. Время ОС не используется как fallback. Исходные receipt timestamps сохраняются через MarketMessage; metadata recorder отделена от торговых часов.

REST на время исполнения заменяется адаптером, отклоняющим любые обращения. Live start/loops не вызываются. Config с включённым event-driven scheduler отклоняется без изменения настройки; активные engine tasks/capture также запрещены. Это локальные ограничения исполняемого пути, не процессная сетевая песочница: создание engine, telemetry и подготовка pre-state остаются обязанностью вызывающего кода.

Выходные recorder events возвращаются как event/symbol/payload без времени записи JSONL. Торговые timestamps внутри payload сравниваются. При передаче `expected_events` сравниваются все события по порядку; ошибка указывает индекс первого расхождения. `outputsMatch=null` означает, что выходы не сравнивались. `parityReady` всегда false.

При ошибке исполнения экземпляр помечается failed: продолжать его нельзя, поскольку мутации стратегии/брокера не откатываются. Ссылки clocks, REST, recorder и capture восстанавливаются при выходе. Ошибки предварительной проверки не изменяют торговое состояние.

## Что подтверждено тестами

Полный набор: **619 passed**, 39.90 секунды в чистом окружении. Добавлены десять тестов offline adapter.

На синтетической последовательности bootstrap → deep snapshot → fast snapshot/delta → trade batch → REST context → arbiter совпадают input observations, все output events и market snapshot. Это включает вложенные evaluations по fail-closed clock gate.

Отдельный тест начинает с одинаковой явно открытой позиции и воспроизводит её защитное закрытие по market trade: совпадают trade_closed, fees, net PnL и баланс. Сам вход позиции в этом тесте является подготовленным checkpoint, не воспроизведённым решением стратегии.

Проверяются повреждённый hash, разрыв sequence, неподдержанный scheduler, interleaving, неправильный clock method, raised scope, повтор сегмента, расхождение выходов и запрет тихого отключения scheduler. Проверяемый market replay запускается с запретом WebSocket adapter и SystemRuntimeClock.

## Следующий обязательный шаг

Полный session dispatcher должен восстановить config/policy/manifests и начальное состояние, воспроизвести scheduler interleaving, clock sync, scanner, control/UI, activation/deactivation и transport worker lifetimes. Текущий API не проверяет provenance pre-state/config, не восстанавливает reconnect и не читает пользовательский JSONL как готовую сессию. Выдавать совпадение одного сегмента за полную parity нельзя.

До этого этапа новый длительный пользовательский прогон не нужен. Затем необходимы capture load validation и полное сравнение кандидатов, отказов, intents, fills/partials и PnL. E01 и допуск к production остаются открытыми.
