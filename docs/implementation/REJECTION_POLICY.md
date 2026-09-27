# Isolated rejection response experiment

Frozen before evaluation: quote_tape_v1 requires directional displacement >=1.5
bps of the executable entry quote AND >=1.5 bps of at least two executions strictly
after the first observed reclaim in the current episode. Only prints available at
the observation fence are used. Existing absorption timing remains; no extra hold
or multi-horizon unanimity is introduced. A new episode resets this research anchor.

Switch: research_rejection_response_policy=quote_tape_v1. Default legacy. Stop,
target, partial, leverage, fee schedule, risk and profile flags are unchanged.
This is a hypothesis, not a defect fix. Synthetic mirrored cases test disagreement,
old tape, future tape and a valid response. Historical causal reclaim quote was
not recorded as a distinct field in v2. Snapshot-only comparison cannot reproduce
this exact candidate without sequential state reconstruction; no historical wins
are claimed eliminated or preserved on that basis.

Payoff controls: existing R01 risk/broker payoff tests and six captured execution
controls retain target, original ETH stop, fees and profitable ZEC partial. Narrow
stops relative to full cycle costs and disabled uneconomic partials remain policy
questions. There is no demonstrated arithmetic discrepancy justifying widened
stops or forced partial. The immediate runner stop following positive partial is
preserved, including the winning control. Portfolio comparison remains unknown.


## Полнота исследования и выплаты

[Популяция](rejection-population.json) включает все 201 сценарий: 120 breakout и
81 rejection; 285 episode-состояний, в том числе 149 rejection. У rejection 50
PREPARED, 11 ready, 5 исполненных и 6 неисполненных ready. У breakout 21 ready,
1 исполнение. Данные не ограничены шестью позициями.

Из 11 rejection-ready gross markout положителен у 6 через 5 с, у 3 через 15/30 с,
у 2 через 60 с и у 4 через 120 с. Это наблюдаемая будущая котировка, не stop/target
исполнение и не прибыль с расходами. Нельзя считать все эти случаи выигрышами
или вычитать отрицательные исходы из общего портфеля.

Точное сравнение quote_tape_v1 по потерянным выигрышам, новым/исчезнувшим входам,
задержке и оставшемуся движению **не завершено**. Причинная котировка первого
reclaim отсутствует в сохранённом состоянии старой политики. Нужен последовательный
прогон state machine с bootstrap, всеми нужными raw/context событиями и обеими
политиками. Присвоение первой котировки из редкого research_frame изменило бы
эксперимент. Это незавершённая реализация сравнения, а не доказательство отсутствия
необходимой информации во всём raw. Альтернативный портфельный PnL неизвестен.

Арифметика по принятым позициям воспроизведена во всех шести controls. XRP T03:
stop 6.2893 bps, partialPlanned=false, ожидаемый net partial −0.18299 USDT,
stop cost share 2.0662. ETH T04: 5.1170 bps, partialPlanned=false, net partial
−0.35282 USDT, stop cost share 2.5396. Узкий stop означает большую долю издержек;
автоматическое включение partial сделало бы эти выплаты отрицательными.

Положительный контроль T01 оставлен: partial и fee-adjusted stop дали
+0.341814 USDT. После partial stop остатка защищает уже положительный цикл;
нет основания отключить его по проигравшим примерам. Risk/UI/broker проверки
условных выплат (R01 и экономика partial) сохраняются. Leverage, target, stop,
partial и включённые стратегии не менялись. Новая политика по умолчанию legacy.
