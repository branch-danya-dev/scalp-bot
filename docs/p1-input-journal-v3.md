# P1-02 v3: чтения часов и границы вычислений

Историческое описание v3. Текущий writer — [v4](p1-input-journal-v4.md); чтение v3 сохранено.

Формат writer — `replay-input-v3`; validator сохраняет чтение v1 и v2 с их собственными coverage. Этот инкремент описывает реально полученные движком значения времени и причинные границы вычислений. Полного offline dispatcher ещё нет, `parityReady=false`.

## Часы

При opt-in capture engine, broker и создаваемые sessions получают `RecordingRuntimeClock`. Каждый вызов `time`, `monotonic`, `perf_counter_ns` записывает метод, возвращённое значение и текущий `scopeId`. Значения не заменяются временем записи и не округляются до миллисекунд. Движение wall time назад сохраняется как наблюдение.

`ClockTape` воспроизводит последовательность вызовов строго: другой метод, отсутствие следующего значения, неверный тип или неизрасходованный остаток вызывают `ClockTapeMismatch`. Системного fallback, автоматического продвижения или ожидания реального времени нет. Ошибочный вызов не потребляет ожидавшее его значение.

Время envelope/input recorder — отдельная эксплуатационная шкала на исходном clock. Она намеренно не проходит через observer, иначе запись чтения часов породила бы новую запись самой себя. Будущий offline dispatcher должен отделять metadata clock recorder от clock tape торгового обработчика. Сетевые receipt timestamps по-прежнему поступают из MarketMessage и не заменяются proxy-часами.

## Scope: причинные границы обработки

`scope.begin` содержит возрастающий ID, `parentId` и имя обработчика. `scope.end` содержит тот же ID и outcome: `returned`, `raised`, `cancelled`. Текущий scope хранится в ContextVar, поэтому параллельные asyncio tasks не делят один глобальный стек.

Покрыты market callback, evaluate, arbiter, event evaluation, bootstrap/context apply, scan, sync, Start/Stop и strategy toggle. Вызовы с именованным аргументом symbol/session также атрибутируются.

Parent ID — причинная связь. Задача может стартовать после завершения вызова, который её создал; это допустимо. Порядок завершения параллельных scopes не обязан быть обратным порядку их начала. Чтение часов с scopeId разрешено только пока сам scope открыт.

Scopes и старые `callback` markers — наблюдения, а не независимые команды повторного исполнения. Если обработчик сам вызывает `_evaluate`, будущий driver обязан сопоставить вложенный scope, а не ещё раз запускать evaluate по его строке. Это правило контракта; готового driver данный инкремент не добавляет.

## Event-driven scheduler

Записываются `scheduled`, `coalesced`, `started`, `sleep`, `resumed`, `finished` с taskId. TaskId закреплён при создании задачи и передаётся ей явно: он не берётся позднее из изменяемого symbol session.

Completion записывается done-callback до удаления задачи из tracked set. Это позволяет увидеть отмену до первого исполнения coroutine: для неё есть `scheduled → finished(cancelled)`, даже когда тело задачи и его finally не запускались. Исключение, которое приложение обработало внутри coroutine, не выдаётся за uncaught task failure; обычные error events сохраняют отдельную диагностику.

Validator проверяет ID/parent scopes, соответствующие окончания, незакрытые scopes у footer, clock reads вне scope, допустимые переходы task lifecycle и незавершённые задачи. Для завершённых scopes/tasks не хранится вся история: возрастающие IDs и набор открытых элементов позволяют обнаруживать повторное использование. Остальные sequence/hash/manifest checks сохраняются.

## Проверки и границы

Полный набор: **599 passed**, 33.54 секунды в чистом окружении без pytest cache.

12 новых тестов: строгая clock tape, сохранение wall rollback и nanoseconds, непотребление при mismatch, legacy v2, параллельные scopes и завершившийся причинный parent, exception/cancel outcomes, ошибки жизненного цикла при корректных row hashes, coalescing, sleep/resume, отмена до старта, named-argument attribution.

Интеграционный тест выбирает tape одного `_evaluate` с недостоверными часами, повторяет его в другом настоящем engine и получает те же WAIT-решения, затем требует полного расходования tape. Это ограниченный fail-closed сценарий; совпадение входов/fills/PnL целой сессии им не доказано.

Остаются: dispatch периодических задач и внешних обращений (включая чтения UI), reconnect/book sequencer state, содержимое внешней research policy. После закрытия этих входов нужен offline dispatcher и автоматический output parity report. Реальная стоимость частой записи clock reads и scope boundaries под нагрузкой не измерена; capture остаётся выключен в стандартном запуске. Новый пользовательский прогон пока не требуется. Торговые thresholds и execution rules не менялись.
