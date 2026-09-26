# P1-03: transport observations и worker lifetime

Следующий инкремент: [service lifecycle](p1-offline-service.md), включая проверку cleanup перед footer. Error paths и REST-await interleaving остаются открытыми.

OfflineScheduledReplay принимает transport rows между scope handlers и на границе окна. ReplayTransport отслеживает worker IDs, symbol lifetime, отдельные topic groups и номера попыток. Реальных соединений, подписок или сетевых таймаутов replay не создаёт: воспроизводится наблюдаемый порядок transport events и применённых market messages.

## Проверка состояния

Live snapshot helper `_transport_book_state` общий с offline проверкой. При каждом transport event сравниваются полные fast/deep bids/asks, depth, synced, updateId и seq, вычисленные общим market handler. Записанные snapshots не загружаются в стакан, чтобы не скрывать потерянные/неправильно применённые сообщения.

Reconnect внутри одного worker сохраняет его OrderBookState. Новому worker ID создаётся отдельный handler с пустыми sequencers; session market state не сбрасывается искусственно. Новый worker того же symbol допускается только после drained всех известных старых каналов. Одновременно активные workers одного symbol отклоняются: market_message в текущем журнале не содержит workerId, и их сообщения нельзя надёжно атрибутировать.

Проверяются допустимые transitions connecting → subscription_sent → fault/cancelled → drained и последующая попытка; штатный drained после subscription_sent тоже разрешён. Topics должны соответствовать symbol и настроенным глубинам, publicTrade/kline. Изменение группировки пересекающихся topics не позволяет обойти drain.

После fault/cancelled очередь может ещё отдавать сообщения до drained — это сохраняет существующий live порядок cleanup. После drained сообщения этого канала отклоняются. `subscription_sent` по-прежнему означает отправку запроса, не ACK биржи.

## Окна и ограничения

Transport state и handlers сохраняются в том же экземпляре adapter между окнами. Первое событие окна может быть transport. При cold origin hash/sequence continuity проверяется как раньше. Открытый сетевой канал на границе такого окна разрешён; отсутствие footer/service shutdown означает, что полнота сессии не подтверждена.

Error type и discarded count — записанные наблюдения. Replay не вычисляет заново число отброшенных transport queue элементов, поскольку сырой ingress до применения целиком не воспроизводится. Scope с OrderBookSequenceError/другим raised outcome пока не является поддержанным восстановлением ошибки. Service start/close тоже остаются открытыми.

## Проверка

Полный набор: **679 passed**, 72.64 секунды в чистом окружении. Добавлены семь тестов transport replay.

Настоящий symbol worker с подставленным transport fixture записывает fast/deep snapshot, fault, delta из очереди, drain, reconnect и новый snapshot. Второй сценарий повторно запускает worker; третий делит запись на несколько непрерывных окон. Cold replay получает одинаковые output events, стаканы и receipt state, сверяя snapshots на всех transport boundaries.

Негативные тесты с корректными hashes меняют book quantity, attempt, workerId или завершают очередь раньше следующего сообщения. Все вызывают отказ и failed engine. В replay запрещён вызов WebSocket adapter.

Торговые правила, disconnect/reset semantics и профиль не менялись. Полный session replay, service lifecycle, error recovery paths и REST-await interleaving ещё не готовы; parityReady=false. Новый длительный прогон не нужен, сервер не перезапускался.
