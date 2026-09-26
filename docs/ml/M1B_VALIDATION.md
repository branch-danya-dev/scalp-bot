# Проверка M1b1

Код: `d764003b6e786eb60a3675329af5828550e7545c`, tree
`d72c3ec16bbd3fa4fedfa04d63c991dab5d513ed` совпадает с локально проверенным.
Основа: ML `b0874d8`; main остаётся `3403b03`.

| Проверка | Результат |
|---|---|
| Новые alignment/features/schema тесты | 58 passed |
| Все tests/ml | 200 passed, 1.25 s |
| Полный scripts/test_preflight.py, Python 3.13.5 | 1332 passed, 129.50 s |
| compileall, JavaScript syntax, diff whitespace | Успех |
| Проверка неизменности runtime imports | В составе полного набора; ML не подключена |

Первый целевой вызов: 56 passed / 1 failed — новая фикстура пыталась менять поле
frozen MarketContext. Исправлена фикстура с созданием следующего context через replace;
код MarketContext и действующие тесты не менялись. Первый полный вызов прерван
лимитом среды 120s, partial log сохранён. Полный повтор: 1331 passed; после отдельной
регрессии согласованности JSON-схемы последний полный запуск дал 1332 passed.
Ни timeout, ни первые ошибочные/промежуточные результаты не выданы за финальный pass.

Свойства: порядок collector-time, атомарные snapshot/tie группы, неоднозначность
межканального порядка, отказ от смешения scope/clock, checksums и запрет overwrite,
границы памяти/записи; реальные классы контекста, units/null/coverage, stale/future
и независимость сохранённого snapshot от следующих изменений исходного context.

GitHub CI и public archive alignment проверяются отдельными jobs и фиксируются
в PR #54 после завершения. Штатные pytest не загружают данные из сети. Sample
workflow по-прежнему сохраняет только metadata report, не raw/normalized vendor data.
Это не обучение, не тест latency worker и не end-to-end raw→features/labels.
Новый торговый прогон и реальные заявки не выполнялись.
