# PR58: завершённое offline-исследование

27 сентября 2026. Продолжение draft PR58 с проверенного remote/local af988946; исходные #54/#57 уже интегрированы. Код и venv из отдельного worktree. Новых market/paper/live сессий, заявок, ML trading, изменения плеча/стопов/partial и merge main не было. Default подтверждения остаётся legacy.

## Решения обычного бота

Последовательная реконструкция **выполнена** через настоящие TradingEngine, ScenarioRouter, WeakLevelRejectionStrategy, PaperBroker и RiskEngine. Первый reclaim взят из живого состояния стратегии после доступного raw-префикса. Bootstrap, свечи, REST-context, scanner metadata, L50/L1000, сделки, controls и transport epoch воспроизведены в порядке source sequence. Исследовательские frames не являются входом. Независимый логический scheduler каждого варианта фиксирует tie policy; REST/network запрещён адаптером.

| Вариант | Подготовка / политика | Эпизоды | Ready | Сделки | Условный net USDT |
|---|---|---:|---:|---:|---:|
| A | один подготовительный владелец / исправленная legacy | 291 | 28 | 5 | -34.367091 |
| B | независимые сценарии / та же legacy | 369 | 37 | 8 | -43.206377 |
| C | независимые сценарии / quote_tape_v1 | 369 | 36 | 8 | -43.530032 |

У всех капитал1000, одинаковые риск/исполнение/комиссии/геометрия и обязательные исправления. Конфигурационный diff проверен: A/B одинаковы, C отличается только opt-in политикой. В каждом портфеле общий риск и один владелец исполнения; это отдельные последовательные ledgers, не сумма ML labels. В конце нет открытых позиций или pending. T01 во всех вариантах сохраняет +0.341814 с partial/stop.

A→B: -8.839285 USDT. По точным object/episode/side ключам 26 ready сохранены, 11 появились, 2 исчезли; четыре входа сохранились, четыре появились, один исчез. Новые исполнения убыточны. Изменение quote-clock episode ID само по себе не доказывает отдельную экономическую возможность. Техническая независимость подготовки подтверждена; историческое улучшение торговли на этой сессии **не подтверждено**.

B→C: -0.323655 USDT. Все восемь исполнений сохранены. Исчез один неисполненный XRP-ready: MFE60s около1.984bps, диагностический net markout -21.596bps. ZEC short задержан на396.021397ms; его фактический net изменился с -11.223909 до -11.547564. Остальные первые ready не сдвинулись. Все11 C rejection-ready удовлетворяют отдельной проверке quote>=1.5bps, tape>=1.5bps и >=2 prints строго после причинного reclaim. Выгода кандидата не доказана; default не менять.

[Таблица всех вариантов](docs/pr58-readiness/routing_policy_comparison.csv), [184 rejection-эпизода объединения](docs/pr58-readiness/rejection_episode_comparison.csv), [портфели](docs/pr58-readiness/portfolio_comparison.json), [provenance/config](docs/pr58-readiness/run-provenance.json). Задержки, оставшееся движение, расходы и точные risk reasons сохранены по эпизодам. Markout — отдельная top-of-book диагностика с13bps расходов, не actual fill PnL.

Исходные201 сценарий, включая81 rejection и32 ready, все сохранены в [coverage](docs/pr58-readiness/baseline_scenario_coverage.csv). Для B/C:104 точных episode match,95 совпадений только объекта,2 исходных non-ready не реконструируются как тот же объект. Среди32 исходных ready только9 имеют точный episode match,23 — совпадение объекта. Это ограничение связи с исходным scheduler, не отсутствие выполненной реконструкции. Отсутствие потерянного прибыльного ledger-входа относится к единственному реализованному выигрышу T01; сохранение всех потенциальных выигрышей на неоднозначных сопоставлениях не доказано.

Полный live counterfactual net остаётся null: fixed captured membership не моделирует альтернативный scanner; ETH gap85.8466354s и неполный transport teardown не исчезают от replay. Условные ledgers выше рассчитаны полностью, но не сертифицируют оригинальный scheduler или отсутствующие котировки. Исторические T01–T06 broker controls отдельно сохраняют исходную арифметику, включая ETH stop. Узкие стопы/partialPlanned=false и экономика выхода не исправлялись входным фильтром.

## Данные и модель

Причина воздержания v1: классы/order/preprocessing/reload проверены; p_target не является max-class confidence. В train12/5212 target_first. Максимум p_target до/после calibration:0.04006/0.07829 на train и0.04207/0.07942 на test. Temperature1.36995 повышает эти малые вероятности; она не является причиной нулевого выбора. Validation AP0.01170 при base rate0.00276: ранжирование существует, но денежное преимущество не установлено. Диапазоны вероятностей и реальные выплаты development/validation сохранены в [диагностике](docs/pr58-readiness/abstention_diagnostics.json). Freshness/геометрия не смешаны с пороговым abstention.

V2 реально обучена:16790 labels из дополнительных morning/noon captures, 278 purged; train6482/cal4160/val3023/test2847. Цель/стоп/горизонт и0.55 неизменны; один CatBoost и logistic, rule/prior/no-trade controls. В train85 target_first, в test13. Структура известна100%, liquidity65.08%; значения берутся из того же MarketContext, а не из будущих отчётов. [Model card](docs/pr58-readiness/MODEL_CARD_V2.md), [coverage](docs/pr58-readiness/context-coverage-v2.json), [оценка](docs/pr58-readiness/evaluation.json), [команды](docs/pr58-readiness/COMMANDS.md).

V2 CatBoost выбрал0 во всех split; logistic на test выбрал2 окна со средним net -0.109132 USDT, правило197 со средним -0.134273. Это перекрывающиеся labels, не портфель. 1200 historical shadow forecasts воздержались; weights/reload и отдельный Windows worker выполнены. Ни logloss, ни число строк не дают торгового допуска. Test заранее отделён по новым labels, но дата ранее изучалась вручную: нетронутый внешний режим отсутствует.

2024 NEAR/XRP не допущены в денежную разметку: [матрица](docs/pr58-readiness/historical_specification_matrix.csv) и [источники/401](docs/pr58-readiness/HISTORICAL_SPECIFICATIONS.md). Расписание Bybit зависит от типа аккаунта и не доказывает числовой minNotional. Данные сентября не переносятся назад. Бесплатный архивный поиск не дал полного набора. Альтернатива выбрана по наличию dated bootstrap, не по прибыльным движениям; именно эти дополнительные периоды вошли в обучение.

## Производительность и транспорт

Профиль нашёл повторный полный обход свечей breach_witness и dataclass serialization в level_ref. Для одинаковых397 evaluations их cumulative время изменилось0.675→0.058s и0.661→0.056s. Результат функции сохранён на случайных историях и контрпримерах. Общий wall time профиля нельзя считать ускорением: SQLite I/O и параллельные jobs различались.

Версии метрик разделяют длительность sleep и lateness относительно запланированного пробуждения. Fixed historical loads используют реальный SessionRecorder/background writer, warmed spawn worker, сохранённый порядок и интервалы, 1x/4x, по3 пары off/shadow. Сохраняются raw samples, counts, p50/p95/p99/max для loop/handler/state/features/queue/predict/return/adapter и CPU/RSS/coalescing. Доставленный abstention не называется выпущенным proposal. Полный отчёт: [latency benchmark](docs/pr58-readiness/latency_benchmark.json).

На первом участке normal p99 укладывается в20ms; burst превышает20ms и без ML. Добавочный shadow budget5ms сохраняется. Второй участок проходит20ms (p99 normal16.41–18.09ms, burst15.72–17.89ms), первый burst20.17–21.84ms не проходит. Data→adapter p99 достигает360.14ms/293.86ms на двух burst-участках, превышая250ms. Predict p99<1ms; source→state достигает357.68ms, то есть очередь обработки образуется до worker. Обнаруженная CPU-избыточность исправлена, **полный performance допуск пока не получен**. Обычный движок benchmark использует исторические логические часы, worker/поступление измеряются wall-clock; реальную WebSocket producer queue и live freshness под OS jitter этот harness не сертифицирует. Benchmark пересекался с локальными offline jobs; результаты не изолируют влияние ОС и фоновой нагрузки. Причина семи исходных handshake failures по этому измерению не определяется.

Новые diagnostics соединения показывают connect_handshake/subscribe_send/receive_or_process/drain, elapsed и безопасную цепочку типов/errno. DNS/TCP/TLS/HTTP внутри connect_handshake отдельно не различаются. Секреты/headers/exception text не записываются. Канонический replay transport input сохранён; recovery/freshness/stale protection не ослаблены. Close timeout решает ограниченное завершение, а не все сетевые разрывы.

## Проверка критериев

| Критерий | Проверка → результат | Код / данные | Ограничение → следующее решение |
|---|---|---|---|
| Причинный reclaim | последовательный raw replay выполнен; C11/11 witnesses pass | offline_study, offline_market_index, CSV | original scheduler parity не сертифицирована |
| Разделение A/B/C | один config, независимые реальные ledgers | portfolio_comparison.json | net отрицателен; default legacy оставить |
| Полный исходный охват | 201/81/32 включены с явным качеством mapping | baseline_scenario_coverage.csv | совпадение объекта не выдавать за эпизод |
| ML pipeline | shared context, dated specs, purge, train/cal/test, reload | dataset/model manifests, context audit | внешние2024 не допущены; независимость режима не доказана |
| Воздержание | классы и calibration проверены; v2 по-прежнему0 | diagnostics/evaluation/model card | торговый допуск отсутствует |
| Latency | реальные повторные off/shadow и сохранённые измерения | latency_benchmark.json | burst20ms остаётся проверяемым ограничением |
| Transport | phase/error diagnostics и mock/replay regressions | bybit.py, transport tests | семь внешних handshake причин неизвестны |
| Регрессии / CI | полный локальный preflight и Linux/Windows на финальном PR head | validation.json, preflight-final.txt | optional skips перечисляются отдельно |
| Артефакты | новые каталоги, реальные weights, checksums, команды | artifact-manifest.json, COMMANDS.md | локальное хранение до удаления владельцем |

Продолжать можно только offline-исследование. Новый рынок, включение ML, main merge и любые заявки требуют отдельного решения. Issue55 остаётся открытым.
