# ML impulse — локальный технический кандидат

27 сентября 2026. M1b1 из PR #54 продолжен причинным датасетом, реальным CPU
обучением и отдельным Windows spawn worker. Торговое подключение выключено.
Оригинальный current-12h остановлен; его аудит завершён, полный replay не подтверждён.

- [Карточка и численные результаты](MODEL_CARD.md).
- [Выполненные команды](experiments/COMMANDS.md).
- [Dataset manifest / coverage / splits](experiments/dataset-manifest.json).
- [Model manifest / hashes / preprocessing](experiments/model-manifest.json).
- [Все метрики](experiments/evaluation.json), [worker](experiments/shadow-report.json),
  [сравнение обычного движка](experiments/engine-shadow-parity.json).
- [Архитектура](ARCHITECTURE.md), [статус этапов](ROADMAP.md),
  [источники и причины исключений](experiments/source-inventory.json).

Обучение выполнено на 10 225 окнах всех семи реально активных монет, а не на шести
сделках. Один уже просмотренный период не доказывает обобщение; test означает
последний временной сегмент разработки, не нетронутый holdout. CatBoost при
фиксированном пороге не предложил входов в test; торговая полезность не доказана.
Структурные/liquidity признаки пока missing, без подмены нулями.

`python -m scalp_bot.ml` показывает возможности установленного кода. Отсутствие
встроенных весов — не отсутствие выполненного эксперимента: веса хранятся локально
в models/ml/impulse-v1-final, их наличие/hash проверяется predict. У обычного
launcher нет импорта optional ML-зависимостей и ожидания прогнозов.
