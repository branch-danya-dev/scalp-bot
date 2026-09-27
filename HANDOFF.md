# Handoff — параллельные сценарии и ML

27 сентября 2026. Ветка `codex/parallel-scenarios-ml-v1`, worktree
`C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot`.
[Draft PR #58](https://github.com/branch-danya-dev/scalp-bot/pull/58).
Интегрированы проверенные heads #57 f0364a5 и #54 c45c7b4, без переписывания веток.
Baseline G:/scalp-bot на 3403b03 чистый; исходный capture hash неизменен.

Обычные стратегии независимо готовятся на символе. Earliest eligible ready получает
общий резерв; время равенства разрешает стабильный tie-break. Один владелец
исполнения/позиции, WAIT/ошибка/отказ другой стратегии не блокируют подготовку.
Сохранены frozen-plan, object/episode и защиты. UI показывает каждый сценарий.

Transport consumer failure обнаруживается при немом recv, reconnect сбрасывает
книгу и подтверждения с требованием snapshot; close timeout #57 сохранён.
Новая rejection quote+tape политика существует отдельно и по умолчанию выключена.
Историческое полное сравнение её альтернативных входов не завершено.

ML: обучены CatBoost и logistic, rule/prior controls, сохранены dataset/weights и
manifests; worker/shadow реально выполнены. Подробности [ML_HANDOFF](ML_HANDOFF.md).
1371 локальный тест прошёл; Linux и Windows ML CI прошли; [IMPLEMENTATION_REVIEW](IMPLEMENTATION_REVIEW.md).
Синтетический off/shadow сохранил ordinary ledger, но event-loop budget20мс не пройден.

Не доказаны: причина всех failed handshakes, независимая модельная полезность,
portfolio PnL маршрутизации/новой политики. Capture остановлен, incomplete seal,
полный baseline replay не сертифицирован; исходник/footer не править.
Новый рынок, реальные заявки, ML trading и main merge требуют отдельного решения.

[Новый контракт](docs/architecture/parallel-scenarios.md),
[транспорт](docs/implementation/TRANSPORT.md),
[исследование политики/выходов](docs/implementation/REJECTION_POLICY.md),
[предыдущий handoff](docs/handoff-before-parallel-20260927.md).
