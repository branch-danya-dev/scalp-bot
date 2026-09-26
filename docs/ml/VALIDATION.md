# Проверка M0

Дата: 26 сентября 2026. Основа — точное дерево main `3403b03`:
`a5781b1659f962b2c0724633c3b93de37f5d0cf9`.

Локально: Python 3.13.5, отдельный virtualenv, объявленные project dependencies
из ранее сохранённых wheel-артефактов, editable install проекта. CatBoost/ONNX/
scikit-learn не устанавливались и не требуются M0.

| Проверка | Результат |
|---|---|
| `python -m pytest tests/ml -q` | 78 passed, 0.08 s |
| `python scripts/test_preflight.py` | 1210 passed, 60.74 s |
| `python -m scalp_bot.ml` | M0_FOUNDATION; trained=false, connected=false, order_authority=false |
| `compileall` новых модулей/тестов | Успех |
| `git diff --check` | Успех |
| Локальные ссылки ML-документов | Все существуют |

Первый полный вызов дал 1207 passed / 3 failed: в новом virtualenv ещё не был
выполнен editable install, и дочерний replay CLI не находил пакет scalp_bot.
Исправлено только окружение (`pip install --no-build-isolation --no-deps -e .`),
производственный код/старые тесты не менялись. Полный запуск повторён успешно;
первоначальный лог сохранён вместе с итоговым, не выдан за проходящий.

Новые проверки: immutable features, явный None, типы/вероятности, версии модели/
схемы/плана, время/epoch/sequence, устаревший источник при свежем predict, все
40 комбинаций фаз и готовности источников, отсутствие runtime imports и status CLI.
Проверка выбора — чистая спецификация; межпроцессный worker, atomic reservation,
ML lifecycle, обучающая выборка, задержки inference и торговая доходность не проверялись,
потому что в M0 они ещё не реализованы. Старый synthetic capture/replay входит
в полный suite; это не новый рыночный прогон.

CI на опубликованном commit проверяется отдельно в PR. Этот документ не объявляет
GitHub CI успешным заранее. Main/current-12h и пользовательская рабочая копия не менялись.
