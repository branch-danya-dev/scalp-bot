# ML handoff — immutable V2, fixed-plan diagnosis

V1/V2, datasets, weights, calibration и threshold0.55 сохранены. Новой модели, labels или обучения V3 в этом этапе нет. [Model card V2](docs/pr58-readiness/MODEL_CARD_V2.md), [исходные manifests/команды](docs/pr58-readiness/COMMANDS.md) и [новый диагноз](docs/pr58-trade-plan/ML_DIAGNOSIS.md).

Причина прежнего воздержания не была ошибкой порядка классов или одной калибровкой: низкая p_target существовала до calibration. Новый анализ проверил экономику фиксированного30/15bps/30s/100ms/100USDT плана, не повторял весь этот аудит. Hashes совпадают; production OrderBookState воспроизводит entry/exit/first barriers для всех13665 development labels. Test2847 и purged278 исключены из новых предсказаний, групп и подбора.

Validation3023 окна/29 зависимых60s групп: mean net−0.131690. Timeout2770: mean−0.120343,81 положительный/2689 отрицательных; target14: mean+0.204721. Timeout не ноль и не автоматически плохой исход. Target30bps по медианному отношению в6.0345 раза больше forming range; MFE до исходного выхода median0.0291bps/p9513.4665bps. Это относится к текущей нарезке, а не ко всем возможностям рынка.

CatBoost top1/5/10% даёт mean net−0.157506/−0.143500/−0.143044, хуже общей выборки; все deciles отрицательны. Upper decile чаще достигает target, но96.03% состоит из ENAUSDT, top5% —100% ENA. Logistic top10%−0.141192; unchanged rule0.55 выбирает258 окон с mean−0.147801; CatBoost/logistic по0.55 выбирают0. Интервалы рассчитаны по зависимым временным группам, пересекающиеся top-срезы не независимы. Суммы label-выплат не портфельный PnL.

Диагноз сочетает пригодность плана/выборки, расходы, слабое денежное ранжирование и ограниченность режимов. Не заставлять V2 торговать. Структурные/liquidity features прежние; неизвестность остаётся явной группой. 2024 NEAR/XRP с неподтверждёнными ограничениями не включать в money labels, September specs назад не переносить.

Все26 причинных ready development-period проверены относительно предыдущей10s-grid точки:23 старше1s или отсутствуют. Это подтверждает рассогласование sampling, не прибыльность пропущенных состояний. [Одна гипотеза](docs/pr58-trade-plan/NEXT_POLICY_HYPOTHESIS.md): отдельный offline эксперимент на first-ready наблюдениях с прежним планом и новым untouched holdout. Ничего не реализовано/обучено под неё. Требуется отдельное решение владельца.

Worker/shadow остаётся без exchange/capital/orders и права вмешиваться в чужую позицию. Обычный launcher прогнозов не ждёт. Isolated performance имеет отдельные stage/pressure/raw артефакты и явный Windows timer profile; соответствие250ms не подтверждает100ms исполнения labels. Полные результаты в [общем отчёте](TRADE_PLAN_DIAGNOSIS.md). M3/live/portfolio admission не следует из технического pass. PR59, launch_authorized=false и selected_mode=null неизменны.
