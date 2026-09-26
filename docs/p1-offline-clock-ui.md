# P1-03: clock sync и побочные эффекты запросов UI

Следующий инкремент: [периодические clock/arbiter loops](p1-offline-periodic.md). Ограничение REST-await interleaving сохраняется.

25 сентября 2026. Оба offline adapter поддерживают корневые scopes clock_sync, public_state и market_health. Восстановленный cold engine может пройти эти участки без пропуска входов и без обращения к REST.

## Общая обработка результатов синхронизации

Live `_sync_clock_once` получает ответ REST и передаёт его `_apply_clock_sample`; ошибку передаёт `_apply_clock_error`. Эти же обработчики вызываются offline внутри исходного clock_sync scope. Формула синхронизации, bounds, RTT/TTL ограничения и логика сохранения предыдущего anchor не изменены.

Replay использует записанные server_ms/sent_mono/received_mono/received_wall_ms. Ошибка восстанавливается как наблюдаемый errorType; динамические классы исключений из строки не создаются, текст сетевой ошибки не нужен. Scope, sample/error markers, clock reads и output events сверяются cursor.

Поддержан завершённый scope с одним результатом: clock_sample или clock_error. Исключение внутри применения sample, которое породило бы оба marker, пока отклоняется. Перемежение других входов во время ожидания REST и periodic clock_loop dispatch ещё не воспроизводятся. Новый код не объявляет такие случаи поддержанными.

## UI влияет на состояние допуска

`public_state` вызывается с исходным selectedSymbol, включая None и отсутствующий символ. Вложенный market_health исполняется самим движком. Поэтому изменения clock reading, сброс решений при invalid clock и события clock_admission_changed повторяются в исходном порядке.

Возвращаемый UI response не записан целиком во входном журнале и не является объектом сравнения этого adapter. Проверяются clock/input observations, торговое состояние и recorder output events; recorderHealth/sessionFile могут отличаться в offline инфраструктуре. Совпадение HTML/UI response не заявляется.

## Проверка

Полный набор: **649 passed**, 54.40 секунды в чистом окружении. Добавлены шесть тестов clock/UI replay.

Синтетическая последовательность начинается с настоящего capture prefix и пустого cold engine: bootstrap → успешная синхронизация → UI → отклонение большого RTT → ошибка REST → market health → TTL expiry → UI/arbiter → новая синхронизация → UI. Проверены оба adapter по scopes и scheduled adapter по всему непрерывному окну.

Совпадают выходные события, engine events с торговыми timestamps, внутреннее состояние MarketClock, clock reading и причина блокировки инструмента. Отказ sample/REST сохраняет предыдущую синхронизацию до TTL; expiry блокирует допуск, новый sample восстанавливает часы. При этом отсутствующие receipt timestamps остаются отсутствующими: recovery не освежает рыночные данные.

Корректно перехешированные изменения server timestamp, errorType или clock method приводят к отказу сравнения и failed engine. Текст транспортной ошибки в capture отсутствует. Это проверка временного допуска и повторения состояния, не доказательство прибыльности стратегии или полного воспроизведения пользовательской сессии.

## Дальше

Остаются periodic/service dispatch, scanner, операторские control, transport/worker lifecycle и ожидания REST с interleaving. Только после прохождения полной записи без пропусков можно проверять session parity fills/partials/PnL и нагрузочную стоимость capture. parityReady=false. Новый пользовательский прогон пока не нужен; runtime-профиль и торговые правила не менялись.
