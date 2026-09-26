# P1-03: частичный сбой контекста и разрыв стакана

Следующий инкремент добавляет [context REST-await interleaving](p1-offline-context-await.md); ограничения ниже описывают состояние на момент этого шага.

Scheduled replay поддерживает два дополнительных пути отказа: per-symbol source_error для context batch и OrderBookSequenceError внутри market_message. Полного покрытия всех source errors и ожиданий REST пока нет.

## Частичный context failure

Записанный тип ошибки представляется неизменяемым SourceFailure — это данные, не класс исключения и не исполняемый код. Общий context loop одинаково обрабатывает реальный Exception и такое наблюдение: записывает исходный errorType/symbol, пропускает применение результата только для этого инструмента и продолжает batch.

История ошибочного инструмента не заменяется пустыми свечами. Успешные результаты других инструментов применяются в прежнем порядке. Следующий успешный batch восстанавливает контекст обычным handler. Текст исключения для этой ветки не нужен и не копируется в input payload.

Поддержаны только ошибки отдельных symbols внутри context results. Global context_error, scanner/bootstrap errors и interleaving во время REST await пока отклоняются. Для scanner/bootstrap старые outputs содержат текст исключения, отсутствующий в source_error input; его нельзя синтезировать по одному errorType.

## OrderBookSequenceError

Offline вызывает общий market handler. Если тот реально выбрасывает OrderBookSequenceError, dispatcher принимает исход только при recorded scope.end outcome=raised и полном потреблении этого scope cursor. Другие исключения, cursor mismatches и несовпадающий outcome не подавляются.

Поэтому book gap воспроизводит исходную очистку sequencer и fast session book. Deep book сохраняется. Последующие transport observations сверяют эти состояния, reconnect и новый snapshot восстанавливают book исходным путём. Исключение не создаётся из текста журнала и состояние не загружается из transport snapshot.

## Проверки

Полный набор: **692 passed** за 82.27 секунды в чистом окружении. Добавлено пять тестов; `git diff --check` проходит.

Partial context fixture сначала оставляет AAA со старой историей при ошибке, успешно обновляет BBB, затем восстанавливает AAA следующим batch. Сравниваются output events и итоговые candles/HTF context; приватный текст ошибки отсутствует в записанных rows.

Gap fixture подаёт delta с пропущенным update ID, фиксирует реальный raised scope, очищенный fast и сохранённый deep book, затем reconnect/snapshot. Offline получает тот же результат. Перехешированные подмены outcome, удаление gap из сообщения и ложный synced transport snapshot вызывают отказ, а не «успешное восстановление».

Следующий шаг — запись/воспроизведение REST request/completion boundaries с interleaving и достаточная диагностика остальных ошибок. Потоковый полный replay, capture load validation и полная торговая parity остаются открытыми. Новый длительный прогон пока не нужен, правила/профиль не менялись, сервер не перезапускался.
