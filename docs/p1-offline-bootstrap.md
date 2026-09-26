# P1-03: холодное восстановление конфигурации и policy

Следующий инкремент: [clock sync/UI replay](p1-offline-clock-ui.md). Полный session dispatcher остаётся в работе.

25 сентября 2026. `restore_cold_engine(prefix)` принимает ровно первые три replay-input-v4 envelope: header, capture manifest и policy snapshot. Возвращает OfflineEngine в состоянии начала capture: торговля выключена, symbols/positions/pending пусты, баланс равен записанному start_balance. Рыночное состояние затем строится через исходные bootstrap и market inputs.

## Допуск

Проверяются schema/body/hash chain, sequence 1–3, capture phase, manifest v3 и полнота public config. Поддерживается только jsonl-clock-v2/paper-v1. Source fingerprint сравнивается с текущими исходниками проекта, runtime fingerprint — с текущим Python/platform/package inventory. Несовместимость отклоняется, режима «продолжить несмотря на различия» нет.

Сравнение кода относится к файлам на диске, как и исходный manifest; это не аттестация загруженного bytecode. Полный inventory runtime намеренно строгий: даже изменение пакета вне торгового пути требует отдельного решения о совместимости в будущем.

Settings создаются с источником только из переданных значений. Environment, dotenv и file secrets исключены. Public fields должны быть полностью классифицированы; результат валидации обязан сохранить config fingerprint. API key/secret всегда пусты. Записанные параметры session_dir, telemetry и policy path сохраняются как данные, но не используются для открытия ресурсов.

Research policy восстанавливается из snapshot в памяти. Проверяются mode, content hash, source hash reference, policy fingerprint и public metadata capture manifest. Исходный файл policy не нужен. Raw-file hash остаётся ссылкой на исходные байты, а не повторной проверкой их форматирования. Enabled strategies берутся из manifest; неизвестные ключи запрещены.

## Изоляция ресурсов

TradingEngine принимает необязательные зависимости REST/recorder/policy и флаг настройки observability; обычный live-конструктор сохраняет прежние defaults. Offline factory передаёт запрещающий REST adapter, memory recorder, восстановленную policy и явные replay clocks; не создаёт HTTP client, каталог сессии, writer thread и telemetry exporter.

OfflineEngine запрещает live start и операторский set_running: control replay пока требует будущего session dispatcher. Закрытие не обращается к сети. Это программные ограничения адаптеров, не изоляция процесса от стороннего кода или ранее запущенной глобальной telemetry.

## Продолжение записи

`replay_origin` хранит исходные fingerprints, hash последней принятой записи и nextSequence. Оба offline adapter проверяют точное продолжение цепочки; пропустить неподдержанный участок нельзя. Config, enabled strategies и policy перепроверяются перед исполнением. До первого сегмента также проверяются пустые sessions/positions/pending, выключенная торговля и стартовый баланс.

После успешного окна origin продвигается; после runtime/output mismatch engine помечается failed. Создание нового adapter поверх него не снимает отказ. Предварительный отказ до мутаций допускает повтор корректного входа. Эти проверки не являются универсальным hash checkpoint всех внутренних объектов: прямую внешнюю мутацию произвольного состояния движка между окнами вызывающий код обязан исключать.

## Проверка и оставшаяся работа

Полный набор: **643 passed**, 55.02 секунды в чистом окружении. Добавлены 14 тестов cold bootstrap и admission.

Тесты запрещают создание live resources, подменяют environment, удаляют исходный policy-файл, проверяют корректно пересчитанные несовместимые manifests и изменения config/policy/strategies/balance. Интеграционный тест восстанавливает engine по настоящему capture prefix и повторяет bootstrap/book/trade/context/arbiter с совпадением output events без ручной подстановки конфигурации.

Factory не загружает полную сессию из JSONL и не восстанавливает checkpoint середины запуска. Сначала нужно реализовать clock/scanner/control/UI/periodic/transport dispatch, чтобы пройти запись без пропусков. Затем — нагрузочная проверка capture и полное сравнение fills/partials/PnL. `parityReady=false`; новый длительный пользовательский прогон пока не нужен.
