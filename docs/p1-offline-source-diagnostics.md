# P1-03: диагностика ошибок scanner/bootstrap

Следующий инкремент: [сквозной service/portfolio fixture](p1-offline-portfolio.md).

Полный набор: **741 passed** за 95.47 секунды в чистом окружении; восемь новых тестов. `git diff --check` проходит.

Экспериментальное событие v4 source_await дополнено фазой `failed` с `errorType` и `errorMessage`. Это точные тип и текст обычного Exception на границе REST-await. ID/source/symbol связывают ошибку с исходным запросом. CancelledError по-прежнему записывается как cancelled; остальные BaseException — как raised и не исполняются offline.

Раньше source_error содержал только тип, хотя scanner/bootstrap outputs уже включали текст. Новая запись закрывает этот пробел, не меняя пользовательскую диагностику. Текст ошибки теперь также присутствует во входном журнале; запись не является обезличенной. Старые raised-записи без диагностики по-прежнему отклоняются, текст не угадывается.

Offline передаёт SourceFailure через один фиксированный RecordedSourceError. Классы исключений не загружаются и не создаются по строке, сообщение не исполняется. Общий source_await проверяет и потребляет failed; общие обработчики scanner/bootstrap сохраняют исходный errorType и текст. Для clock остаётся ранее определённый ready + clock_error без копирования текста REST-ошибки.

Поддержаны scanner failure в periodic loop и service start; bootstrap failure внутри scanner, включая startup. Ошибка scanner сохраняет прежний ranking/активные инструменты; bootstrap failure не активирует незагруженный инструмент. Уже выполненная ротация не откатывается. Следующий успешный scan очищает scanner error и применяет результаты обычным путём.

Восемь новых тестов проверяют ошибки, повторный успешный scan, startup/close/footer, совпадение outputs/engine events и состояния. Перехешированные изменения ID/source/errorType/errorMessage отклоняются по границам или сравнению outputs. Текст сверяется только при предоставлении expected_events; hash chain сам по себе не доказывает подлинность записи.

Не покрыты необработанная ошибка отдельного root scan без внешнего loop/startup, ошибки применения результата после await, global context errors, прочие market errors и rejected controls. Streaming/load validation и полная торговая session parity остаются открытыми. Следующий шаг — проверка полноты оставшихся путей отказа и сквозной торговый fixture с позициями перед новым длительным capture.

Правила торговли и профиль запуска не менялись; сервер не перезапускался, новый прогон пока не нужен.
