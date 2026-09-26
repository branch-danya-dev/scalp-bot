# P1-03: ожидания scanner и bootstrap

Следующий инкремент: [clock REST-await](p1-offline-clock-await.md).

Позднее добавлена [фаза failed и воспроизводимая диагностика](p1-offline-source-diagnostics.md); старый raised без текста остаётся неподдержанным.

Scheduled replay приостанавливает scanner и bootstrap на записанной границе загрузки, исполняет промежуточные market/UI/поддержанные задачи и продолжает после ответа. Поддержаны scan как отдельный вызов, periodic scanner loop и первоначальный scan внутри service start.

## Контракт

Экспериментальная v4 дополнена `source_await`: `id`, `source` (`scanner` или `bootstrap`), `phase` (`wait`, `ready`, `cancelled`, `raised`). Symbol обязателен только для bootstrap. ID возрастают на протяжении жизни engine. Валидатор связывает завершение с исходным ID/source/symbol и запрещает незавершённые ожидания перед footer.

Live и offline используют общий `source_await` context manager. Scanner обрамляет вызов active_candidates; bootstrap — исходный gather metadata, fees и свечей. Граница ready записывается до применения результата. Порядок live-запросов и обработки результатов сохранён; отдельные HTTP retries и порядок ответов внутри gather не воспроизводятся.

При `cancelled` offline получает CancelledError. Отмена scanner не применяет новый ranking; отмена bootstrap не активирует незагруженный инструмент. Предшествующие эффекты scanner, включая уже выполненную ротацию, не откатываются. Segment adapter принимает только соседние wait/ready; interleaving требует scheduled adapter. Старые записи без границ сохраняют прежний ограниченный путь, source/runtime binding не ослабляется.

## Ограничения

`raised` фиксируется в live-журнале и структурно проверяется, но offline пока отказывается от его исполнения. Текущие scanner/bootstrap error outputs содержат текст исключения, которого нет в source_error input. Восстанавливать его по типу исключения нельзя; диагноз и формат безопасной записи остаются отдельной задачей.

Поддержаны bootstrap, вызванные общим scanner, а не произвольный отдельный bootstrap из неизвестного mid-session состояния. Незавершённые корутины между окнами не переносятся. Полная торговая session parity, clock REST-await, глобальные context errors и streaming/load validation ещё не готовы.

## Проверки

Полный набор: **722 passed** за 87.95 секунды в чистом окружении; 14 новых тестов. `git diff --check` проходит.

Задержанные scanner и bootstrap пропускают snapshot стакана существующего инструмента и public_state. Проверены успешное завершение и отмена на каждой стадии для отдельного scan и periodic loop. Сверяются outputs, ranking, состав активных инструментов, свечи, стакан и engine events; offline не вызывает сеть, asyncio Tasks/gather/sleep.

Перехешированные ID/symbol, пропущенный ready и неподдержанный raised отвергаются. Отдельно проверен структурный lifecycle. Существующие scanner rotation и service start/close fixtures сохраняют совпадение.

Следующий шаг — clock REST-await и воспроизводимая диагностика source errors. Затем streaming/load validation и полное сравнение торговой сессии. Правила входа/выхода и профиль запуска не менялись; сервер не перезапускался, новый длительный прогон пока не нужен.
