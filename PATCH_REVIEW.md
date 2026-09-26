# Patch review: stopped two-hour current-12h

27 сентября 2026. Ветка **codex/two-hour-run-audit**, база **3403b03**.
Исправляющий коммит **1f40b1a**; отчёт и остальные таблицы — следующий коммит ветки.
Diff: `git diff 3403b03..codex/two-hour-run-audit`.
Main и ML/PR #54 не менялись. Новых рыночных запусков нет.

## Исправления

| Дефект | Before → after | Контроль |
|---|---|---|
| Подтверждение rejection живёт дольше причинного эпизода | Новый прокол наследует старое поглощение/окно ленты → absorption/fire сбрасываются при новом episodeKey. Arm/pin/подготовка, лимиты и использованные поколения сохраняются | Реальные 3328 XRP prints + контекст O161914: baseline LONG, patch WAIT. Тот же эпизод сохраняет LONG; синтетические long/short снова разрешают вход после собственного подтверждения |
| Frozen ready теряет фазу | WAIT после risk refusal возвращает ARMED в PREPARED → phase остаётся ARMED, WAIT неторгуем, frozen/firstSignal неизменны | Настоящий ScenarioRouter: ready → risk reject → WAIT → повторный ready. Сохранение фазы не создаёт заявку |
| Close транспорта дольше shutdown engine | Websockets default 10 s против engine 5 s → explicit close_timeout=2 s | Настоящие stream/engine classes и offline slow peer: исходный TimeoutError исчез; cancelled/drained завершены. Заведомо неотменяемая задача по-прежнему делает capture incomplete |

Продуктивный diff: три файла, 19 добавленных строк / одна заменённая.
Не менялись торговые пороги, профиль, стратегии, leverage, RiskEngine, fees,
формулы стопов/targets/partial или денежная аллокация. Close timeout — бюджет
освобождения ресурса, не свежесть рыночного сигнала.

## Baseline и граница доказательств

[Полный аудит](TWO_HOUR_RUN_REVIEW.md).
Capture: `G:/scalp-bot/data/paper-captures/current-12h-20260926-222654-704-f604f5f6`.
80 source-файлов совпали с baseline; runtime packages совпали. Штатная проверка
replay проведена до изменений: отказ из-за incomplete seal. Весь gzip/chain
проверен: 26 566 351 inputs; единственная категория нарушения — unfinished
transport attempts, семь fast channels. Ранняя ручная остановка сама по себе
не объявлена повреждением.

Дополнительное ограниченное выполнение настоящего OfflineScheduledReplay на
исходной версии сопоставило **1334 output events** в первых 100000 inputs,
после чего встретило искусственно обрезанный root scope. Это граница окна,
не обнаруженная порча оригинала и не полный replay. Дополнительная попытка
scheduler по всему индексу остановлена без полного результата; она не используется
для расчёта PnL или заявления parity. Диагностические процессы завершены.

Фактическая бухгалтерия и causal snapshots пригодны для локальных проверок.
**Новый портфельный PnL неизвестен.** Нельзя вычесть потерю XRP из старого итога:
изменятся капитал, последующие сигналы и замещающие сделки. Capture.json/footer
не «ремонтировались», валидатор не ослаблен.

## Проверки

- Новые регрессии на неизменённом 3403b03: **5 failed / 1 passed**.
- После исправлений: **12 passed** — шесть указанных выше и шесть исторических
  PaperBroker-контролей. Сохранены T01 maker partial и прибыль; ETH original stop;
  совпали exit/gross/fees/funding/net всех шести принятых позиций.
- Стратегии/router: **108 passed** после первых двух исправлений.
- Capture + новые регрессии после всех правок: **40 passed**.
- **Полный набор: 1144 passed за 257,61 с**, Python 3.13.15, зависимости capture.
- Синтаксис: AST parse **205 Python-файлов**, `node --check` **5 JS-файлов**.

Первый полный запуск: 1137 passed / 7 failed из-за среды worktree: subprocess
импортировал editable baseline из общей venv, а sandbox запретил временный
data/sessions. Исправлена команда проверки: PYTHONPATH указывает на audit-worktree,
тестам разрешены временные записи. Исходники и проверки ради этого не ослаблялись.
Повторный полный запуск прошёл. [Логи](docs/run-reviews/two-hour-20260926/validation/).

Повторение из этого checkout в PowerShell:

```powershell
$env:PYTHONPATH = (Get-Location).Path
& G:/scalp-bot/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

Полный набор включает owner pending/position, double orders, staged entries,
freshness, protective exits, synthetic capture/replay и денежные ограничения.
Это локальные тесты, не запуск биржевого сервиса.

## Остаточные вопросы

Исправление XRP не доказывает качество всей гипотезы rejection. Ложные
микрореакции, дорогой маленький stop, выключенный partial и дальний target
остаются вопросами торговой политики. Опубликованы все 26 неисполненных ready
и 206 событий независимого ценового экрана, включая отрицательные исходы.

ETH получил новые назначения: вечный generation block и ущерб от scanner не
подтверждены. Но ~85,85 с не было fast-book: backpressure и семь failed handshakes.
Close timeout исправляет конфликт бюджетов, а не доказывает исправление всей сети.

Issue #55 остаётся открытым до review и решения владельца. Main не сливать,
новый run автоматически не назначать.
[Предыдущий PATCH_REVIEW](docs/scenario-router-patch-review-2026-09-26.md).
