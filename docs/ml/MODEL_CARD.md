# Model card — trend_impulse_ml

Статус: TRAINED_TECHNICAL / OFFLINE_SHADOW / NOT_ADMITTED / NO_ORDER_AUTHORITY.
Версия `catboost-impulse-v1:d901a0d0e55b33cc`. Веса действительно обучены на CPU и повторно загружены;
точное совпадение прогнозов проверено. Это ограниченный первый кандидат, не торговый допуск.

| Поле | Значение |
|---|---|
| Веса | `C:\Users\workingspace\.codex\worktrees\parallel-scenarios-ml\scalp-bot\models\ml\impulse-v1-final/model.cbm` |
| SHA256 model | `d901a0d0e55b33cc6bbe4212c9f91fa4b11f645bf2f27aaf321468f7662178c4` |
| SHA256 dataset | `d41d66da5dd8746c779cad43798ab7f95b3e39f4c8de19ac153ceb84cec6f4b1` |
| Schema | `market-context-v1:dd86ef9b9197904ed2516bbcadbb173331155b49ade52d3394701685ae70601c` |
| План | ImpulsePlanPolicyV1.1: nominal 100 USDT, stop 15 bps, target 30 bps, 30 с, без partial |
| Исполнение labels | Taker после 100 мс, qty/price rounding, исполнимая сторона/depth, fee 0.055% на сторону + 1 bps slippage на fill |
| Модель | CatBoost 100×depth4, lr .05, seed1729, CPU threads1; logistic C1 и fixed rule/prior controls |
| Preprocessing | Median/mean/scale только train, missing flags; side отдельный вход, всего 100 transformed columns |
| Calibration | Temperature только calibration split, параметры сохранены |
| Срок хранения | Локально до удаления владельцем, автоматического удаления нет; веса/raw не в Git |

Источник: один остановленный capture, 2.118164 часа пригодного sampling-span,
семь реально активированных USDT-linear монет. 10 225 labels: target96, stop726,
timeout9403. Train5212/cal1448/val1450/test1807, purged308; book gap исключил102,
правое цензурирование41. Фиксированное sampling10с обеих сторон, прогрев60с.
Временные границы общие для всех монет; удаляется полный будущий label interval и
пересекающий границу 60-секундный causal bucket. Labels перекрываются внутри split,
поэтому число строк не равно числу независимых наблюдений.

| Контроль / test | Log loss | Brier | ECE | Выбрано / 1807 |
|---|---:|---:|---:|---:|
| catboost | 0.419513 | 0.232542 | 0.075186 | 0 |
| logistic | 0.500817 | 0.239401 | 0.077311 | 1 |
| fixed_rule | 0.730969 | 0.407380 | 0.184366 | 187 |
| train_prior | 0.621738 | 0.290784 | 0.116986 | 0 |

Порог p_target>=0.55 зафиксирован до оценки и не понижался после abstention.
Logistic выбрал 1 окно с net −0.303161 USDT; rule187 со средним −0.116563 USDT.
Это независимые nominal labels с перекрытием капитала, не доходность портфеля.
Средняя train-выплата timeout −0.123299 USDT: вероятность timeout не означает нулевой
денежный исход. CatBoost estimated net по train class payouts −0.131252 USDT;
без условной модели выплат это только диагностическая оценка.

На 200 исторических test snapshots: все прогнозы воздержались, latency готовые
features→adapter p50=3.0725/p95=3.1324/p99=3.2600 мс, submit p99=.072 мс,
RSS97.17 МиБ, worker CPU0.797 с включая0.688 с startup, clean shutdown.
На synthetic движке: общий evaluate→worker→adapter p99=64.3295 мс (8 forecasts),
обычные решения/ledger совпали; event-loop p99 off54.2782/shadow53.5689 мс.
Бюджет20мс НЕ пройден. Измерение короткое, включает startup fixture и не доказывает
steady-state на всех монетах. CPU Ryzen5 7600X, 6C/12T, RAM31.1GiB, Windows11/Python3.13.15.

## Ограничения допуска

Все даты источника уже проверялись в аудите; test не независимый holdout. Нет
многопериодной статистики, confidence intervals, проверенной portfolio delta-net
или drawdown. Structure/liquidity feature groups missing, свойства внешней истории
не подтверждены текущим REST. Модель не доказала полезных новых входов; M2/M3 в
полноценном смысле не закрыты. Отчёты по монетам/выборкам, все hashes и параметры —
в experiments/*.json. Стратегии, плечо и live/paper разрешения не изменены.
