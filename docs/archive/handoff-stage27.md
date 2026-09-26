> Historical snapshot. Current status: [HANDOFF](../../HANDOFF.md); plan: [production roadmap](../production-roadmap.md).

# Текущее состояние проекта

- Репозиторий: `branch-danya-dev/scalp-bot`.
- Основная ветка: `main`.
- Текущий технический HEAD перед созданием этого handoff: `ad49dabfe85701711cdcdc72e4b6011be5ea542c`.
- Проект находится в paper-trading режиме. Live execution пока не подключён.
- Активные торговые playbooks:
  - `level_breakout`;
  - `weak_level_rejection`.
- `orderbook_density` используется только как liquidity evidence и самостоятельно сделки не открывает.
- `trend_structure` отключён до отдельной проверки edge.
- Breakout/rejection staged adds реализованы инфраструктурно, но для текущего профиля отключены.
- Текущая market-data архитектура:
  - Bybit `orderbook.50` — fast book для best bid/ask, spread, OFI, stop/target triggers и latency-sensitive strategy path;
  - Bybit `orderbook.1000` — deep book для density, liquidity walls, VWAP, stop-side stress и depth-aware fills;
  - L50 и L1000 имеют независимые websocket connections, sequence/sync/freshness состояния.
- Strategy evaluation для engaged setups event-driven; polling `0.20s` и arbiter `0.25s` оставлены как fallback.
- WebSocket ingest:
  - `recv(decode=False)`;
  - reusable `msgspec.Decoder`;
  - typed `MarketMessage`;
  - bounded `asyncio.Queue`;
  - отдельный market processor;
  - при опасном backlog — reconnect/resnapshot, а не drop orderbook deltas.
- Recorder пишет JSONL через отдельный background writer queue/thread и не блокирует market-data loop.
- Добавлена latency observability:
  - Prometheus histograms;
  - `/metrics`;
  - OpenTelemetry traces;
  - Grafana + Tempo + OTel Collector stack;
  - timestamps от exchange receipt до paper fill.
- Подготовлен Stage 27 для контролируемого 10-часового прогона:
  - `SCALP_PAPER_RUN_DURATION_SECONDS=36000`;
  - research frames каждые 3s;
  - trade tape в research frames записывается как `delta_v1`;
  - replay/UI frames не дублируют trade tape;
  - post-run анализ строится как небольшой overview + почасовые shards.
- Запуск 10h:
  ```powershell
  .\scripts\run-research-10h.ps1
  ```
- Сбор результата после прогона:
  ```powershell
  .\scripts\build-latest-10h-pack.ps1
  ```

# Последние коммиты

- `ad49dabfe85701711cdcdc72e4b6011be5ea542c` — Stage 27: подготовка 10h run, delta trade tape, latency snapshot в run summary, overview + hourly shard analysis bundle.
- `9bc67fc95929506cc3981683e338e063ad465ea3` — Stage 26: end-to-end latency observability, Prometheus/OpenTelemetry/Grafana/Tempo.
- `2e5202008da8915ae12945bc5bd71057a918db79` — Stage 25: `msgspec`, decoupled websocket ingest, bounded market queues, recorder background IO.
- `5a3425c75a399ef5f56e4ca075835ea2851d5e3f` — Stage 24: dual L50/L1000 books и event-driven market hot path.
- `b03c4e6fa70089c31e4d332ae50d413b5a807b27` — Stage 23: закрытие P1 strategy/execution gaps.
- `712ad8ea0d8eca5e627050164eb51d2c5c59b7cb` — Stage 22: усиление breakout/rejection semantics и session-level structural setups.

# Что уже проверено

- Последний Stage 27 PR и post-merge workflow прошли успешно: **437 Python tests passed**, JavaScript syntax checks зелёные.
- Проверена causal semantics breakout:
  - post-retest response считается только после retest;
  - pre-retest rolling move не может сам вызвать FIRE.
- Проверена causal semantics weak rejection:
  - response считается после absorption;
  - требуется post-event micro response;
  - старое движение до absorption не засчитывается.
- Проверена structural identity:
  - detector drift не создаёт новую breakout generation;
  - rejection использует generation-based `setup_id`;
  - current UTC day high/low сохраняют одну session identity при продолжении импульса.
- Проверена economics/risk consistency:
  - stop-side depth stress участвует в sizing;
  - stressed risk сохраняется в portfolio/open-risk accounting;
  - maker fill больше не считается полным от одного маленького trade-through print.
- Проверена dual-book архитектура:
  - L50 управляет fast executable path;
  - L1000 остаётся источником depth/liquidity;
  - stale/desynced fast book и deep book обрабатываются независимо;
  - deep-only updates не спамят strategy evaluation.
- Проверен event-driven FIRE path:
  - engaged setup может пройти evaluate без ожидания 0.20s polling;
  - новый FIRE может сразу вызвать arbiter без ожидания 0.25s arbiter tick;
  - significant-event gating и coalescing ограничивают частоту тяжёлых evaluate.
- Проверен decoupled ingest:
  - websocket reader продолжает принимать сообщения при занятом callback;
  - stale backlog не обрабатывается как свежий market state;
  - recorder IO вынесен из market loop.
- Проверена latency telemetry:
  - `exchange → receive`;
  - `receive → parse`;
  - `parse → processor`;
  - `book → features`;
  - `parse → strategy`;
  - `strategy → FIRE`;
  - `FIRE → order`;
  - `order → ack/fill`;
  - exact newly-fired setup correlation.
- Проверена Stage 27 long-run запись:
  - delta trade tape сохраняет все новые prints между research frames;
  - offline replay восстанавливает rolling tape из delta frames;
  - при discontinuity выставляется `tradeDeltaGap=true`;
  - sharded pack создаёт overview и часовые ZIP-файлы.

# Найденные проблемы

- Закрыто: L1000 использовался одновременно как fast-price source и deep-liquidity source, добавляя до ~200ms source latency в hot path.
- Закрыто: strategy evaluation и arbiter были в основном polling-driven и могли суммарно добавлять сотни миллисекунд перед входом.
- Закрыто: websocket reader блокировался на `await callback(message)`, поэтому тяжёлая strategy/recorder работа могла задерживать приём следующих WS сообщений.
- Закрыто: recorder делал serialization/disk IO слишком близко к trading loop.
- Закрыто: breakout/rejection могли использовать rolling micro-response, частично сформированный до causal event.
- Закрыто: breakout identity зависела от geometry/center и могла повторно торговать ту же lifecycle generation после detector drift.
- Закрыто: rejection strategy identity и engine/broker setup identity были разными.
- Закрыто: moving current-day high/low мог создавать новые setups внутри одного продолжающегося impulse.
- Закрыто: risk cap учитывал фиксированный stop slippage, но не стресс по реальной stop-side depth.
- Закрыто: maker simulator мог считать весь order filled после одного малого trade-through print.
- Закрыто: long-run frames многократно дублировали один и тот же rolling `recentTrades`, сильно раздувая JSONL.
- Закрыто: старый 10h profile ограничивал `recentTrades` количеством prints, из-за чего на активном символе 80 trades могли не покрывать 60s flow horizon.
- Открыто для проверки прогоном: реальный expectancy breakout/rejection после всех semantic/execution исправлений.
- Открыто для проверки прогоном: насколько event-driven path реально уменьшил p50/p95/p99 decision latency на живом Bybit feed.
- Открыто для проверки прогоном: есть ли систематическая задержка на конкретной стадии `receive/parse/features/strategy/arbiter/execution`.
- Открыто для проверки прогоном: насколько conservative maker queue model занижает fills и насколько близка paper execution economics к реалистичной.
- Открыто для проверки прогоном: остаются ли wrong-direction, late-entry, false-FIRE или target/stop placement ошибки на реальных 10 часах рынка.
- Открыто для будущего live execution: instrument metadata (`tickSize`, `qtyStep`, `minNotional`, contract/status filters) и реальные private-order ACK/fill timestamps ещё не являются частью live trading path.

# Принятые архитектурные решения

- Не выбирать между быстрым и глубоким стаканом: использовать одновременно fast L50 и context/depth L1000.
- Fast book отвечает за реакцию; deep book отвечает за liquidity/depth economics.
- FIRE path — event-driven; polling остаётся fallback.
- Не запускать heavy evaluate на каждый book delta: использовать significant-event gating + 50ms coalescing.
- Не терять orderbook deltas при перегрузе очереди: fail-fast reconnect/resnapshot вместо drop-oldest/drop-newest.
- JSON parsing в hot path — через `msgspec` и typed envelope.
- Socket reader, market processor и recorder IO разделены.
- Prometheus labels ограничены bounded-cardinality полями; `setup_id/event_id/trace_id` не используются как labels.
- Конкретная causal correlation хранится в OTel trace/event ID и в session JSONL.
- Paper execution latency явно помечается как `paper_taker` / `paper_maker`; не считать её реальным Bybit private-order RTT.
- Research trade tape для длинного прогона хранить как delta sequence, а не repeated rolling snapshots.
- Raw session JSONL остаётся локальным lossless source of truth.
- Для больших прогонов не делать один монолитный upload:
  - сначала `*-overview.zip`;
  - затем только нужные `hour-XX.zip` shards.
- Long-run overview должен содержать:
  - global session report;
  - critical events;
  - latency summary;
  - final Prometheus histogram snapshot;
  - shard index.
- Hourly shards должны сохранять causal trade tape, 3s market frames и более глубокий DOM вокруг важных событий.

# Что делаем следующим шагом после прогона

1. Дождаться полного 10-часового auto-stop и убедиться, что записан `run_summary`, нет recorder errors/dropped rows и нет критических `tradeDeltaGap`.
2. Остановить сервер и выполнить:
   ```powershell
   .\scripts\build-latest-10h-pack.ps1
   ```
3. Сначала анализировать только `*-overview.zip`.
4. В первом проходе проверить:
   - итоговый net/gross PnL и комиссии;
   - breakout vs rejection;
   - long vs short;
   - regime breakdown;
   - realized R, MFE/MAE, partial/runner lifecycle;
   - exit reasons;
   - wrong-direction / late-entry / missed-opportunity clusters;
   - false FIRE / risk rejects / arbiter blocks;
   - p50/p95/p99 latency по всем стадиям;
   - market queue lag, recorder health и data continuity.
5. По `shard-index.json` определить часы, где были:
   - крупные убытки;
   - сильные пропущенные импульсы;
   - latency spikes;
   - серии stop-outs;
   - подозрительные maker fills/cancels;
   - strategy/data errors.
6. Подгружать только соответствующие `hour-XX.zip` и проводить causal разбор tick/DOM/strategy state для этих интервалов.
7. По результату разделить выводы на:
   - strategy edge;
   - entry timing/latency;
   - execution economics;
   - risk/position lifecycle;
   - data/telemetry integrity.
8. После анализа вносить только подтверждённые данными правки и запускать следующий сравнительный paper run на том же формате записи.
