# Текущее состояние

27 сентября 2026. Аудит остановленного current-12h завершён.
Ветка **codex/two-hour-run-audit**, база **3403b03**, код/регрессии **1f40b1a**.
Worktree: `C:/Users/workingspace/.codex/worktrees/two-hour-audit/scalp-bot`.
Baseline `G:/scalp-bot` сохранён. Main и ML/PR #54 не изменялись.

Capture `current-12h-20260926-222654-704-f604f5f6`: 22:30:53 → 00:38:41 МСК,
**2:07:48,188**, шесть сделок, баланс **959,267881**, net **−40,732119 USDT**.
Все закрыты по стопу до ручного Stop. Gzip/hash/sequence целы; seal incomplete
из-за семи незавершённых fast WebSocket attempts. Полный replay не сертифицирован;
новый портфельный PnL после патча неизвестен.

## Исправлено

- XRP: новый эпизод наследовал старое поглощение; доказано на реальных 3328 prints.
  Новый episodeKey сбрасывает absorption/fire, сохраняя подготовку и ограничения.
- ARMED не возвращается в PREPARED после risk refusal + WAIT; frozen/firstSignal
  сохраняются, WAIT не создаёт заявку.
- Close timeout 2 с укладывается в существующий shutdown 5 с. Действительно
  незавершённый teardown по-прежнему не получает sealed.
- **1144 tests passed**, синтаксис 205 Python / 5 JS. Защитные выходы всех шести
  позиций и прибыльный partial T01 воспроизведены и сохранены.

ETH: initial stop 2668,67 сработал по bid 2668,66, без переноса/partial.
Новые rejection-сценарии назначались; вечный generation block не найден.
В **23:48:45–23:50:11** было ~85,85 с без fast-book. Поздняя отмена ETH:20:
anchor 2664,79, range_abs 0,875, цена 2667,47 > 2667,415. Scanner держал 7/12
слотов, вытеснения другого кандидата не установлено.

## Продолжение

[Аудит](TWO_HOUR_RUN_REVIEW.md), [patch/test review](PATCH_REVIEW.md),
[таблицы, evidence index и графики](docs/run-reviews/two-hour-20260926/).
Raw, переиспользуемые SQLite-индексы и рабочие скрипты анализа остаются локально
в `G:/scalp-bot/data/audit-two-hour-20260927`, в Git их нет.

Рассмотреть патч и отдельные гипотезы: micro-response, экономика маленького
stop/partial, сопровождение после partial, покрытие тренда, причины failed handshakes.
Торговые пороги/профили не менялись. Issue #55 открыт.
**Не сливать main и не назначать/запускать новый рынок без решения владельца.**

[Предыдущий handoff](docs/handoff-before-two-hour-audit-2026-09-27.md).
[R01–R05](docs/scenario-router-remediation.md).
