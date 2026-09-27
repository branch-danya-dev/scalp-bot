# Статус этапов после реализации 27 сентября 2026

| Этап | Реализовано и выполнено | Незакрытый критерий |
|---|---|---|
| M0/M1a/M1b1 | Контракты, внешние readers/alignment, существующие 49 признаков сохранены из #54 | Внешний format smoke не считается модельным test |
| M1b2 | Raw capture→общие candle/flow/context функции→FeatureSnapshot; executable labels; global purged splits; manifests | Внешняя историческая metadata/coverage не подтверждена; два feature family missing |
| M2 technical | CatBoost и logistic действительно обучены, rule/prior controls, calibration, веса, exact reload, команды predict/evaluate | Нет независимых периодов и доказанной торговой полезности; полноценный M2 не закрыт |
| M3 offline | Windows spawn worker, bounded latest mailbox, fault/TTL/schema tests, 200 исторических прогнозов, обычный движок off/shadow с тем же ledger | Event-loop p99 выше заранее заданных 20 мс; ограниченная synthetic нагрузка не даёт production допуска |
| M4/M5 | Не выполнялись | ML-торговля, новый paper/live рынок и portfolio admission требуют отдельного решения |

Конкретное продолжение: подтвердить внешнюю instrument metadata и период покрытия,
построить независимый заранее выбранный временной holdout; получить достаточную
нагрузочную трассу steady-state с основным ботом, не смягчая лимит после измерения.
Новые торговые фильтры, риск/плечо, состав профиля и main не переключались.
Все выполненные команды и артефакты перечислены в experiments/COMMANDS.md.
