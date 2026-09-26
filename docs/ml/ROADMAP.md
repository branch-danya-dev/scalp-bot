# План до первой тестируемой версии

Один инкремент = конкретные файлы, тесты, артефакт и критерий выхода. Нет обязательного
календарного многонедельного цикла или нового 12h-прогона для каждого инкремента.
Текущую запись не изменять. Переход к реальным ордерам не входит ни в один этап.

| Этап | Работа / результат | Критерий завершения |
|---|---|---|
| **M0 — сделано в этой ветке** | Анализ базы, решения чата, контракты snapshot/forecast, таблица выбора, статус CLI и тесты | Нет imports/hooks в trading runtime, новых зависимостей/изменений профилей; тесты проходят |
| **M1a — реализован** | Bybit trades / Tardis trades+L2 readers, UTC/decimal normalization, source/hash manifest, format tests | Полные файлы читаются потоком, ошибки не превращаются в успешную выборку; engine не затронут |
| **M1b1 — реализован** | Склейка Tardis trade/L2 imports по collector time, quality/provenance и чистый MarketContext→FeatureSnapshot | Локальные регрессии; без runtime и без утверждения полного dataset |
| **M1b2 — следующий** | Coverage внешних архивов и capture, metadata/universe, raw→context adapter, sampling/labels и purged splits | Согласованные единицы/время/покрытие; будущая информация не попадает во входы; raw не заменён старым списком сделок |
| M2 — первая обученная offline-модель | Simple rule + logistic baseline + CatBoost; calibration, frozen artifact и model card | Training воспроизводим; нетронутый test и uncertainty; код inference работает локально. Отрицательный результат не маскируется |
| M3 — v0.1-shadow | Local worker, bounded mailbox, журнал прогнозов, feature parity и fault/load tests | Ошибки ML не влияют на rule/защиты; source age и p99 под пределом, off-mode сохраняет baseline; НЕТ ордеров ML |
| M4 — v0.2-paper candidate | ImpulsePlanPolicyV1, импульсный object/episode, atomic dispatcher, единый risk/broker | Реальные классы открывают/ведут обе стороны на synthetic и пригодном replay; нет двойных заявок/перехвата/обхода risk; baseline/hybrid сравнимы |
| M5 — первая общая paper-проверка | Только по отдельному решению владельца: frozen model+config, ограниченная сессия, полный журнал | Полный отчёт технической и торговой полезности; принять/отклонить/недостаточно данных, не автоматически запускать следующую сессию |

**Первая рабочая модель для тестов — M2 offline**, а не сегодняшние dataclass.
Первая работа рядом с ботом без торговли — M3. Первый тест собственных ордеров
модели в общем paper-портфеле — M4/M5, после проверки предыдущих границ.

## Следующая конкретная задача M1b2

M1b1 описан в [M1B_ALIGNMENT_FEATURES.md](M1B_ALIGNMENT_FEATURES.md). Общий
адаптер уже существует, но raw→MarketContext и feature/label exporter ещё нужны.

Работу с внешними архивами и синтетическими проверками можно вести сейчас в
изолированной среде. Собственный current-12h подключается после завершения
и штатной проверки на своих исходниках; ждать его для разработки readers не нужно.

1. Зафиксировать inventory всех доступных источников: путь/hash/source commit,
   schema, universe, периоды и качество. Непроверенный current-12h не объявлять valid.
2. Использовать схему M1b1; зафиксировать ImpulsePlanPolicyV1 и численные labels,
   sampling и purged time boundaries ДО обучения. Сохранить proposal→frozen историю.
3. Реализовать raw→MarketContext и offline extraction с достаточным префиксом;
   переиспользовать готовый context feature adapter, без второго scanner.
4. Тесты: одинаковые input→features offline/online; future event не влияет на прошлое;
   forming→closed, активация/деактивация, gaps, nullable fields, обе стороны labels,
   расходов не вычитают дважды; повреждение не превращается в отрицательный sample.
5. Выдать компактный dataset manifest, coverage/class report и команды построения.
   Не коммитить raw/parquet/weights. Не заявлять training завершённым на этом этапе.

Если источников для честной разметки недостаточно, указать конкретное отсутствующее
поле/окно/покрытие. Не заменять весь этап предложением ещё одного произвольного прогона.

## Что отложено намеренно

Общий модельный trend для rule-стратегий, learned exits/size, RL, online learning,
автоматический выбор монет моделью, нейросеть/ONNX, возвращение density в самостоятельную
стратегию, live deployment. Это отдельные гипотезы, не скрытые требования M0–M5.

## Изоляция разработки

Ветка `feat/ml-impulse-foundation` от `3403b03`; root ML_HANDOFF дополняет, не заменяет
обычный HANDOFF. Не делать git switch/pull в каталоге работающей записи. Дальнейшая
разработка — отдельный worktree и virtualenv, после завершения текущего capture либо
на другой машине. Ни M0 CLI, ни документация не запускают market loop/обучение.
