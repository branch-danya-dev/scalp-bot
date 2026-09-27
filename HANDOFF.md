# Trading model handoff — 28.09.2026

Ветка `codex/trading-model-admission`, [draft PR #60](https://github.com/branch-danya-dev/scalp-bot/pull/60). База `dfcc5949f2b5cb6f902e304dfbe5bd1f4a7b3132`; проверенный implementation commit `e0a291f111c97d133d634adb6ab303b63885ecc0`. Main не изменён. Реальных mainnet/Demo заявок и 8h/12h прогонов не было.

## Состояние

P0 реализован: scenario больше не заменяет HTF/flow/local policy; финальный dispatch повторно проверяет текущий MarketContext. Контекстная инвалидация breakout/rejection возвращена с 3s debounce по свежим наблюдениям; owner не меняется, hard stops сохранены. PreparedIntent → EconomicPlan → FIRE разделены; несколько независимых символов допускаются за один pass с пересчётом бюджета после каждого резервирования.

P1: добавлены сегменты strategy × trend relation × local regime × HTF × flow × target source × stop distance × cost share и режимы expectancy off/shadow/enforce. Default — shadow, минимум 100 валидных уникальных закрытых позиций; неполные сегменты не запрещают вход. Conditional target payout не объявляется expectancy. Hash/encoding capture перенесены в bounded FIFO writer; market message detach p99 0.224 ms в финальном smoke. Pending maker tick использует узкий evaluator с порядком fill → invalidation. Windows IPC receive вынесен из market loop в bounded reply relay; consumer уступает управление между FIFO-сообщениями. Лимиты очередей, fees, slippage, stress и risk caps не увеличивались.

P2 — исследовательская основа, а не готовая обученная модель: first-prepared collector и advisory forecast contract, purged walk-forward/embargo/leave-symbol-out, заранее зафиксированный протокол. Собрано 11 + 1 реальных first-prepared FeatureSnapshot; это features без executable labels, `trainingReady=false`. V3 не обучена и не включена; её runtime ranker/veto adapter ещё предстоит реализовать и проверить. V2 weights и threshold 0.55 сохранены. Worker не импортирует exchange/broker/risk; обычный admission остаётся единственным путём к исполнению.

## Коммиты и проверки

- `dc7c092` — аудит и implementation plan по файлам/тестам.
- `8abbf3b` — восстановление MarketContext и post-fill context loss.
- `0bde9a4` — writer hash/encoding вне market consumer.
- `764b8db` — admission, multi-symbol budgets, сегменты, V3 contracts/protocol, smoke tooling.
- `25aa629` — IPC reply relay, FIFO fairness, отсутствие ссылок на recorder/engine в отложенных строках.
- `e0a291f` — runner-target frequencies и сохранение неизвестных outcome flags.

Регрессии подтверждённых дефектов сначала падали; receipts сохранены в рабочем каталоге. Финальный Windows preflight: **1506 passed** (226.33 s). Windows ML/spawn CI-equivalent: **313 passed** (14.28 s). Linux/Python 3.12: **1497 passed, 4 skipped** (95.29 s); опциональные ML-зависимости проверены на Windows. JS syntax — passed. [GitHub CI 36354743216](https://github.com/branch-danya-dev/scalp-bot/actions/runs/36354743216): Linux и Windows success.

Первый Linux запуск на Windows mount дал timeout/cache-permission ошибки; повтор на native ext4 прошёл без изменения тестовых таймаутов. Проверено совпадение всех 128 Python-файлов Linux-копии с веткой. Для локального Windows venv из исходного checkout требуется `PYTHONPATH` на изолированный clone; иначе CLI может импортировать старый editable package.

## Технические smoke и replay

| Проверка | До последних hot-path fixes | Текущий head |
|---|---:|---:|
| Публичный paper интервал | 300 s | 300 s |
| Market messages | 141412 | 110657 |
| First-prepared snapshots | 11 | 1 |
| Естественные fills | 1 | 0 |
| Закрытый portfolio net, USDT | -1.225867 | 0, торговли не было |
| Loop p99, ms; budget 20 | 48.0222 — fail | 17.4498 — pass |
| Data→adapter p99, ms; budget 250 | 823.3360 — fail | 123.4093 — pass |
| Input rows written | 2163327 | 1585794 |
| Backpressure / writer drops | 0 / 0 | 0 / 0 |
| Input chain/scope checks | passed | passed |
| Risk rejections before FIRE | 17/17 | 1/1 |

Первый FIRE имел `contextAllowed=true`, `netAtTarget=5.7681`; сделка NEARUSDT weak_level_rejection закрыта по duration_elapsed. Финальный live smoke **торгово неопределённый: 0 fills**; FIRE→order на этом head в live не измерен. Предыдущий FIRE→order был 3.0477 ms. Два периода различаются нагрузкой, поэтому улучшение p99 не выдаётся за строгий performance A/B. Сохраняются отдельные loop outliers до 236.74 ms; GC не отключался и лимиты не повышались. Короткая диагностика обнаружила main-thread Windows pipe polling и отдельные GC паузы; не все причины исторических сетевых ошибок установлены.

Текущий код отдельно исполнил естественный setup на записи первого smoke: **1 вход, 1 закрытие по weak_level_context_lost, net -0.669728 USDT**, открытых/pending остатков нет. Это counterfactual replay с собственным логическим scheduler, не native output parity и не доказательство улучшения PnL. Его результат не складывается с live PnL и ready labels.

## Edge и решение

Исторический parallel_legacy: breakout 3 закрытия / net -1.439775; rejection 5 / net -41.766602, все пять countertrend. Covered ready controls: rejection countertrend 0 положительных illustrative net60 из 11; countertrend breakout в этой выборке отсутствует. Это уже изученные development-периоды и малая выборка. Автоматический veto или удаление стратегии не введены. Подробности: [historical evidence](docs/trading-model-historical-evidence.md).

План 30/15 bps / 30s для V3 не перенесён автоматически: медиана quote MFE60 15.53 bps для breakout и 2.74 bps для rejection, но нет достаточных depth/fill labels для подбора новых чисел. Основной V3 plan — frozen structural plan стратегии; альтернативы выбираются только на training, до validation. [V3 protocol](docs/ml-v3-prepared-protocol.md).

## После smoke

1. Не запускать 8h по факту зелёного CI или нулевого net. Текущий live fill gate остаётся непроверенным; не ослаблять economics ради активности.
2. Развить V3 features в executable labels с depth, maker evidence, partial/runner/costs и censoring. Зафиксировать global wall-time provenance для объединения capture; текущие mono domains не склеивать. Нужны независимые периоды/символы; PR58 и эти smoke уже development.
3. Обучение, calibration, purged walk-forward и leave-symbol-out; только затем проверенный ranker/veto adapter через общий AdmissionEngine. Ни source/model selection, ни threshold не подбирать на holdout.
4. Перед A/B проверить общий feed/scanner, независимые ledgers, один бюджет на ветвь, остановку обеих ветвей при loss/DD 30 USDT и нулевую дополнительную DD B. Эти experiment-controller guards должны быть реализованы и проверены: исторический rule-profile оставлен с `enforce_session_loss_limit=false`.
5. [Новый 8h-протокол](docs/trading-model-next-8h-protocol.md) и [паспорт](docs/trading-model-evidence/next-8h-passport.json) инертны, `launchAuthorized=false`. Model V3 hash и полный B config hash — null, не вымышленные значения. Старый PR59 не менялся; ranker не выдаётся за прежний критерий самостоятельных ML-сделок.

## Provenance и артефакты

- Source on disk SHA256: `feced7f2ce49d673dd9ab08747ebf64dfd60db85f9f57aeaf855a625438c9f91`.
- Smoke config SHA256: `3a4dc624021235fb3425b00d2936ecbcbf0183d7304f17d9a7580c56c0764a44`.
- Planned common rule-config A/B SHA256: `cef3b2a317b13b58f3ca491a05a9783ad87bc44d8acfee67e042886f3f36ff0b`. Полный B ещё не определён.
- Diagnostic V2 model SHA256: `a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2`; manifest SHA256: `15b55fa2c360729cebb37d8cb9cbe432249907873d7016b3b81929df173082f3`.
- Runtime SHA256: `af5748c639af3cc554ab410df665b424d29d51dc1f2a9c93b2da8df0dd4ca9d5`.

Компактные результаты и hashes: [evidence summary](docs/trading-model-evidence/summary.json). Подробные пользовательские отчёты лежат в `outputs/` текущего чата. Сырые captures: `work/paper-smoke-300s`, `work/paper-smoke-post-300s`, диагностический `work/paper-smoke-diagnostic-60s`; replay — `work/postfix-replay-first-smoke`. Корень чата: `G:/codex/2026-09-28/referenced-chatgpt-conversation-this-is-an`. Исходные данные `G:/scalp-bot/data` не изменялись. Не удалять raw, failed attempts или source receipts при следующем этапе.

---

# Исторические записи до текущего этапа

Прежние разрешения на Demo и другие запуски относятся к описанным ниже этапам, не к этой задаче.

# Latest handoff — private Demo heartbeat diagnosis

Повторный Demo завершился в 22:12 МСК с private_ws_gap после примерно 10 минут без сделок; финальная сверка прошла. Найден и исправлен отсутствующий прикладной heartbeat Bybit; добавлена диагностика этапов/close codes без секретов. Причина конкретного старого разрыва журналом не сохранена; VPN пользователь не менял. [Разбор](docs/demo-paper-1h/PRIVATE_HEARTBEAT_FIX.md). Ordinary paper продолжает свой час; новый Demo-час автоматически не запущен. Проверка двух приватных соединений завершена без заявок: старый клиент снова оборвался через 640.40 s; исправленный выдержал 660.41 s и 32/32 прикладных pong. Серверный close reason отсутствует, поэтому вывод ограничен этой парой. 73 targeted и полный CI 36344057891 прошли. Receipt и hashes — в разборе.

---

# Latest handoff — Demo execution repair and authorized start

Исправлены два фактических дефекта количества ZEC, потеря определённости create rejection и повторная работа при финальной сверке. [Разбор](docs/demo-paper-1h/EXECUTION_REPAIR.md). Последнее поручение владельца разрешает фактически запустить обычный paper и Bybit Demo; mainnet заявки и merge main остаются запрещены. Проверка аккаунта перед повтором подтвердила отсутствие открытых позиций и заявок. Оба режима фактически запущены 27.09.2026 в 21:51 МСК и подтверждены работающими на 21:53:19: ordinary UI running/market ready, Demo accepting/сверка аккаунта/реальные прогнозы worker. Час ещё продолжается; завершение/fills/PnL не объявлены проверенными. 1470 полных локальных тестов, повторный launcher preflight и Linux/Windows CI 36341896376 прошли. Артефакты и время запуска — в разборе и receipt. Старые captures и результаты не переписаны.

---

# Latest handoff — owner startup clock repair

Исправлена преждевременная остановка пустого Demo/paper-прогона после первого отклонённого clock sample: теперь входы закрыты до подтверждённой синхронизации, повторная проверка работает внутри исходного часа. При наличии резервирования/заявки/позиции потеря часов по-прежнему запускает безопасное завершение. Лимиты 400 ms, freshness и паспорт не менялись. [Разбор и проверки](docs/demo-paper-1h/STARTUP_CLOCK_FIX.md).

Первый ручной запуск владельца прошёл Demo preflight, но завершился с нулём пар; исходные журналы сохранены. 77 регрессий прошли. Повторного рыночного запуска агент не выполнял. Следующие разделы описывают прежний этап подготовки, до этой попытки владельца.

---

# Current handoff — Demo / paper preparation

Новый режим `demo-paper-execution-1h-v1` реализован отдельно от обычного запуска и PR59. [Протокол и ручной Start](DEMO_PAPER_1H_PROTOCOL.md), [implementation review](DEMO_PAPER_IMPLEMENTATION_REVIEW.md), [паспорт](docs/demo-paper-1h/passport.json), [проверки](docs/demo-paper-1h/VALIDATION.md).

Один scanner/MarketContext/arbiter, парное резервирование и две независимые execution queues/ledgers. Оба контура получают одинаковое исходное количество; фактические fills, комиссии, partial и остатки принадлежат каждому исполнителю. Символ освобождается только после завершения и сверки обоих. Demo-only REST/private WS, без fallback, cancel-all, изменения плеча или чужих заявок. Stop/TP семантика заранее описана: локальные исполнимые bid/ask плюс явные resting maker limits; нет подмены биржевым LastPrice-stop.

V2 подключается через отдельный research adapter с прежними .55, сеткой 10 секунд и планом 30/15/30. Обычный готовый предварительно оценённый план сохраняет приоритет. Worker не получает секретов или брокера, защита остаётся в родительском процессе. 60 реальных прогнозов сохранённых весов снова воздержались; synthetic long/short fills существуют только в contract fixtures.

Локального `.env.demo-paper.local` нет. Его заполняет владелец на компьютере по пустому шаблону; ключ не передавать в чат. Connected preflight и отдельный Start не выполнялись. Часовой результат, преимущество Demo и торговая полезность ML пока не измерены. Режим по умолчанию выключен; код/тесты/CI не запускают рынок. PR59 `next-paper-8h-v1`, 28800 секунд и его цели не менялись.

Артефакты подготовки: `G:/scalp-bot/data/audit-demo-paper-preparation`. Исходный raw, baseline main, предыдущие отчёты, datasets и V1/V2 сохранены. Следующий раздел — завершённая предшествующая диагностика, её ограничения продолжают действовать.

---

# Handoff — PR58 trade-plan diagnosis

Ветка `codex/parallel-scenarios-ml-v1`, [draft PR58](https://github.com/branch-danya-dev/scalp-bot/pull/58). Текущий этап описан в [TRADE_PLAN_DIAGNOSIS.md](TRADE_PLAN_DIAGNOSIS.md); предыдущий [readiness review](PR58_READINESS_REVIEW.md) и его исходные отчёты сохранены. Worktree/venv: `C:/Users/workingspace/.codex/worktrees/parallel-scenarios-ml/scalp-bot`.

Разобраны все 11 уникальных путей исполнения (21 наблюдение вариантов, 9 точных episode identities). Ухудшение single_legacy → parallel_legacy: новые −19.371309, исчезнувшая +10.165085, сохранившиеся +0.366938 = −8.839285 USDT, остаток 0. Сопоставление соседних ZEC-эпизодов не объявляется экономической независимостью. Положительный контроль и все пригодные ready сохранены. Причины — неустойчивый контртрендовый разворот, недостаточный масштаб относительно полного цикла расходов, ограничения плана/сопровождения и ETH gap; это не автоматические новые фильтры.

Подтверждённый дефект: полученная до открытия позиции пачка publicTrade могла задним числом подтвердить maker partial. Исправление исключает её только из maker-exit evidence; сохраняет рыночные ticks, book marks, защитный stop и свежие подтверждения. Регрессия до патча падала, после проходит. Старые ledgers не переписаны; post-fix портфельный результат не рассчитывался и улучшение net не заявлено.

Изолированный старый benchmark сохранил превышения. Длинные обработчики совпали с generation-2 GC над preload-архивом. Исправлен harness: архив изолирован до создания runtime, GC работающих объектов сохранён; очередь и стадии связаны ID, исходная дата доступности не сбрасывается. Полные24 запуска прошли20/5/250 мс: максимальные p99 loop18.4703, добавка2.6548, data→adapter216.01465 мс. Исходные failures и raw сохранены. Запрос Windows timer1ms не улучшил idle p99 около15 мс; launcher неизменён. В V2 burst до6.34% ответов медленнее100ms — допуск исполнения labels не доказан. Результат зависит от указанной среды; это не допуск всей сети или автоматическая настройка launcher. Причины семи исторических handshake failures остаются неизвестными.

V2 и её datasets/weights неизменны. Новый аудит плана и validation не показал денежно полезного ранжирования; подробности в [ML handoff](ML_HANDOFF.md). Единственная предложенная гипотеза — отдельная проверка момента наблюдения по первому причинному ready, с прежним планом/риском; пока только на согласование.

Новые артефакты: `G:/scalp-bot/data/audit-pr58-trade-plan`; прежние `G:/scalp-bot/data/pr58-completion`, raw и baseline main3403b03 сохранены. Большие локальные файлы хранить до явного решения владельца; пути и checksums перечислены в evidence index. Параллельная подготовка, один владелец позиции, общий риск и legacy default сохранены.

Не доказаны улучшение обычного/гибридного портфеля, независимый holdout, live исполнение и причины всех сетевых разрывов. ETH85.8466s gap, fixed captured membership, incomplete teardown остаются. Issue55 не закрывать. Новые market/paper/live прогоны, реальные заявки, ML trading и merge main не разрешены. PR59 цели/28800s неизменны; launch_authorized=false, selected_mode=null. Ничего автоматически не запускать.
