# P1-02: bootstrap, REST-контекст, scanner и manifest

История второго инкремента. Текущий writer: [v3 — clock reads и processing scopes](p1-input-journal-v3.md).

25 сентября 2026. Формат захвата расширен до `replay-input-v2`. Чтение старого `replay-input-v1` сохранено с его прежним списком отсутствующего покрытия; смешивание версий в одной цепочке отклоняется. `parityReady` всё ещё false: scheduler, transport reconnect и полный порядок чтений часов пока не воспроизводятся.

## Добавленные входы

| Kind | Содержимое и момент записи |
|---|---|
| `bootstrap` | Полные результаты REST gather перед применением: instrument, fee schedule, исходные 1m/5m/15m/1h candles, включая неподтверждённые |
| `rest_context` | Полученные свечи до merge/filter/mutation: отсутствие запроса 1m сохраняется как null, пустой ответ — как [] |
| `scanner_result` | Полный упорядоченный список Candidate, включая mark/funding/activity поля; пустой результат тоже записывается |
| `source_error` | Тип ошибки bootstrap/context/scanner и источник, без текста исключения |
| `symbol_lifecycle` | Активация после применения bootstrap и запрос деактивации перед очисткой состояния |
| `manifest` | Полный проверяемый public manifest при создании capture и при каждом принятом Start |
| `run_end` | Manifest ID/hash и причина закрытия торгового прогона |

Candle сохраняется с `start_ms` и исходным `confirmed`, без округления до секунд. Instrument/fee/candidate поля ограничены схемой; лишние поля не проходят запись/валидацию. Instrument и fee schedule должны соответствовать symbol. Снимок делается до передачи входа логике, поскольку reconciliation может менять `confirmed` у полученных объектов.

Bootstrap и REST merge теперь применяются через отдельные синхронные обработчики `_apply_bootstrap_result` и `_apply_context_result`. Рабочий runtime вызывает те же обработчики после REST. Они не обращаются к сети; интеграционный тест восстанавливает объекты из записанного body, повторно применяет их к другому engine и сравнивает итоговый market snapshot. Это проверка конкретного участка применения состояния, не replay всего прогона.

## Привязка конфигурации

Capture manifest следует сразу после header и описывает конфигурацию/код/зависимости ещё до прогрева. Run manifest связывает последующее торговое окно с его фактическим Start; `run_end` закрывает именно этот ID/hash. Public manifests строятся существующей allowlist, `.env` и ключи не копируются.

Валидатор проверяет внутренние hashes manifest, совпадение config/code/runtime provenance capture и run, отсутствие перекрывающихся прогонов и повторного использования ID, правильную ссылку `run_end`, закрытие активного прогона перед footer. Переименование/замена настроек с пересчитанной цепочкой строк не скрывает расхождение с capture manifest. Стратегии могут переключаться зарегистрированными control events; список enabled между capture и Start не обязан совпадать.

Ограничения: source-on-disk по-прежнему не доказывает идентичность загруженного bytecode. Hash chain не является подписью. Наличие привязанного manifest не заменяет полного начального состояния. Содержимое внешнего research-policy artifact отдельно пока не записывается. `captureManifestChecked` и `boundRuns` в отчёте относятся к этой input-цепочке; проверка обычных Start/Summary остаётся задачей session-integrity.

Для структурно целой v2-цепочки без capture manifest отчёт добавляет `capture_manifest_missing` в missingCoverage. Отсутствие зарегистрированных прогонов само по себе допустимо для захвата до нажатия Start; это не подтверждение торговой parity.

## Оставшееся покрытие

1. Все значимые чтения runtime clock, включая изменения watermark MarketClock внутри callback.
2. Dispatch/completion/coalescing и причинная связь вложенных async callbacks.
3. Состояние транспорта и book sequencer при reconnect/reset.
4. Полное содержимое применённого research-policy artifact, если политика используется.

После этого — offline dispatcher и сравнение выходов engine/broker. Следующий практический шаг: запись clock observations и scheduler boundaries так, чтобы вложенное вычисление не запускалось повторно при чтении журнала. Новый пользовательский прогон пока не требуется: capture остаётся opt-in API и выключен в стандартном launcher. Реальная стоимость полного захвата под нагрузкой ещё не измерена.

## Проверки

Полный набор: **587 passed**, 47.70 секунды в чистом окружении без pytest cache.

Новые тесты проверяют backward compatibility v1, применение bootstrap/REST через общий путь без сети, сохранение входа до мутации, порядок кандидатов scanner, отсутствие текста исключения в input journal, реальную связку engine Start/Stop с manifests, конфигурационное расхождение при корректных hashes строк, неправильный run_end, повторный ID, незакрытый run и отказ от лишних metadata fields. Торговые правила и профиль не менялись.
