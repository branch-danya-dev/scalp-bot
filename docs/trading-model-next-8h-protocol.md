# Следующий 8h A/B: обычные setups и ML V3 ranker/veto

Статус: подготовка; запуск запрещён до закрытия технических и модельных gates. Только PaperBroker, без Demo/mainnet. Длительность 28 800 секунд от общего монотонного Start; сетевые паузы входят в это время. Продление ради сделок или прибыли запрещено.

## Предмет сравнения

A — исправленные обычные стратегии, сегментная expectancy-диагностика в shadow. B — те же causal prepared intents и обычные правила, дополненные заранее обученным и зафиксированным V3 ranker/veto. Модель не создаёт сторону, target, заявку или второй риск-бюджет. Каждый оставшийся кандидат заново проходит MarketContext, EconomicPlan и PortfolioRisk перед FIRE.

Стартовый баланс каждого портфеля — 1000 USDT. Для контроллера эксперимента зафиксированы stop по net-убытку 30 USDT и equity drawdown 30 USDT на каждую ветвь; дополнительная допустимая просадка B относительно A — 0 USDT. Контроллер останавливает обе ветви при любом из этих нарушений. Исторический rule-profile имеет `enforce_session_loss_limit=false`; его hash не подменяется, поэтому отдельный внешний loss/drawdown-контроллер обязателен и должен пройти тесты до Start. Поток, scanner/membership, часы, комиссии, slippage, stress, ownership и лимиты идентичны. Ledgers A/B независимы; внутри каждой ветви один бюджет и один execution owner на символ. Сделки, вытесненные ранжированием или veto, входят в разницу полных портфелей.

Это новая постановка сравнения, соответствующая поручению о V3. Прежний `next-paper-8h-v1` из PR59 требовал собственных самостоятельных ML-сделок. Ranker/veto не выдаётся за выполнение того критерия G2; PR59 не переписан и не запущен. Вариант B недоступен без модели и проверенного адаптера. Shadow без изменения реально исполняемых решений не доказывает торговую полезность.

## До допуска

- Все P0/P1 регрессии, полный preflight Linux/Windows и ML/spawn CI пройдены.
- Короткий capture-mode smoke содержит естественные fills, целую hash-chain, нулевые critical drops/backpressure, event-loop p99 <=20 ms и data-to-adapter p99 <=250 ms; для ML добавка к loop p99 <=5 ms относительно контроля. Отсутствующая стадия не помечается PASS.
- Экономические отказы возникают на PREPARED_INTENT; каждый FIRE имеет текущий EconomicPlan и полную текущую проверку MarketContext. Один символ не имеет конкурирующих execution owners.
- V3 labels имеют реальное depth/fill/cost происхождение; подготовлены purged walk-forward, embargo и leave-symbol-out, отдельный calibration и untouched test. Зафиксированы model/schema/dataset/split/calibration hashes и порог. Модель до этих gates отсутствует, её hash остаётся null.
- Проверен A/B runner: общий порядок событий, независимый учёт, резервирование, штатное закрытие, модельные ошибки/expiry. Наличие протокола не означает готовность runner.
- Паспорт содержит exact source/config/runtime/model hashes, список owners, все пределы риска и указанные пределы убытка/просадки. Hash полного B остаётся null до фиксации модели и адаптера; совпадение hashes обычной конфигурации A/B не означает готовность полного B. Незаполненное поле блокирует Start.

## Метрики и остановка

Основные результаты: сверенные net_A, net_B, delta_net=B-A после всех legs, частичных выходов, комиссий, funding и закрытия в конце. Положительность проверяется до округления, epsilon=0.000001 USDT. Требуются естественные исполнения в A и наблюдаемый вклад ranker/veto в B. Положительный net одного периода не доказывает устойчивый edge; ноль сделок — NOT_TESTED/INCONCLUSIVE.

Показать win rate, expectancy R, MFE/MAE, target/partial/stop/no-follow-through по зафиксированным сегментам, число и причины veto, ready→economic→FIRE→fill funnel, часы, symbols/regimes и концентрацию. Параллельные labels не складываются в portfolio PnL. Неизвестные данные сохраняются как missing/censored.

При нарушении hash-chain, потере causal ordering, critical drops/backpressure, неизвестном состоянии позиции, нарушении риск-лимита или недоступности обязательного ML-adapter прекратить новые входы обеих ветвей, отменить pending и штатно закрыть позиции; сохранить весь результат и причину. Не переходить B→A скрытно. Стартовый технический провал не заменять автоматически новым 8h запуском.

В конце — запрет новых входов, отмена pending, закрытие остатков с реальными costs и сверка в заранее ограниченном окне 90 секунд. Неизвестная экспозиция блокирует финальное утверждение net. Код, веса, параметры, состав стратегий и метрики после Start неизменны. Автоперезапуск выключен.

## Обязательные артефакты

Паспорт и evidence-index с SHA256; два ledger; portfolio comparison; strategy/segment reports; hourly equity; scenario/admission funnel; data health; stage latency; модельные прогнозы/veto с source/intent identity и временем. Сырые captures и исходники сохраняются отдельно от Git. Результаты: MET, NOT_MET, NOT_TESTED, INCOMPLETE или INVALID_FOR_CLAIM, отдельно деньги и техническая пригодность.

Проверенный source commit: `e0a291f111c97d133d634adb6ab303b63885ecc0`; on-disk source SHA256 `feced7f2ce49d673dd9ab08747ebf64dfd60db85f9f57aeaf855a625438c9f91`. Exact hashes и незакрытые gates — в [паспорте](trading-model-evidence/next-8h-passport.json). Финальный live smoke: latency budgets passed, 0 fills; 8h не разрешён.
