# Транспорт: воспроизводимые дефекты и границы

Исправления #57 сохранены, включая close_timeout=2 при бюджете shutdown 5 с.
Consumer error теперь немедленно прерывает ожидающий recv, даже когда сервер
замолчал. Ошибка очереди больше не скрывается до следующего пакета/35-секундного
таймаута. Размер очереди и freshness не увеличены, deltas не прореживаются.

На connecting/fault/cancelled движок сбрасывает готовность соответствующей книги,
а fast-поток также очищает tape/OFI, решения и старые подтверждения. Новые deltas
без snapshot непригодны. Владение исполнением и защитные выходы сохраняются.
Сброс действует и без включённого capture. В новых записях сброс находится в
transport_invalidate scope; replay воспроизводит причинный родительский scope.
Manifest v5 добавляет исследовательский переключатель; v4 читает исходный набор полей.

## Измерения

[Полные числа](transport-load.json), команда:
`python scripts/check-transport-load.py G:/scalp-bot/data/audit-two-hour-20260927/inputs.sqlite docs/implementation/transport-load.json`.
Использованы 62 сохранённых L50-пакета, последние 2 с processing cadence перед
ETH gap, bounded range I15000020..I15031824. Это измерение общей оценки и lifecycle
router, не всего обработчика стратегии и не сетевой симулятор.

| Проверка | single router | parallel router |
|---|---:|---:|
| handler p99, мс | 0.4032 | 0.5993 |
| queue lag p99, мс | 0.0472 | 0.0340 |
| максимальная очередь | 1 | 1 |
| искусственный handler sleep 80 мс | backpressure после 11 пакетов | backpressure после 11 пакетов |
| очередь при перегрузке | 20 | 20 |

При перегрузке consumer заканчивается MarketDataBackpressureError; тест отдельной
немой связи проверяет немедленное обнаружение ошибки, tests/test_transport_recovery.py.
Shutdown/реально незавершённый teardown проверяются существующими localhost-тестами;
ложный drained/footer не создаётся.

В исходной записи queue lag p99 этого участка 103.0133 мс, depth p99=23,
parse p99=0.0133 мс. Отсутствуют раздельные старые затраты writer/context/strategies,
поэтому источник всей задержки по этим полям не определяется.

ETH gap: I15031782→I15268623, 85.8466354 с; подробные попытки с phase/error/sequence —
[исходные transport events](../run-reviews/two-hour-20260926/transport_events.json),
[разрывы котировок](../run-reviews/two-hour-20260926/quote_gaps.json).
Семь failed handshakes не объяснены close_timeout. Нет DNS/TLS/сетевой трассы,
позволяющей разделить недоступность сети и сервера. Версия о VPN не доказана.
Исходный incomplete capture не исправлялся; полный replay не сертифицирован.

## PR58 completion evidence

The new phase/error diagnostics record connect_handshake, subscribe_send, receive_or_process and drain; handshake subphases remain unknown. Only error class/errno and host are retained, never exception text or credentials. Diagnostics are recorded separately from canonical replay transport input to preserve clock-read parity. Mock phase failures and offline parity regressions pass. Full repeated historical measurements use the production recorder: [readiness](../../PR58_READINESS_REVIEW.md). Old measurements above are retained, not overwritten.
