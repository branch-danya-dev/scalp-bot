"""Owner-started 24h ordinary trading. No model, replay or research launch gate."""
import argparse
import asyncio
from collections import deque
from contextlib import suppress
import json
from pathlib import Path
import time

from pydantic import SecretStr

from .config import Settings
from .engine import TradingEngine
from .cross_venue import CrossVenueRuntime
from .cross_venue_public import PublicCrossVenueService
from .demo_paper.contracts import SafetyError
from .demo_paper.engine import PairedEngine
from .demo_paper.preflight import PublicRest, load_credentials, connected_preflight, PUBLIC_REST, PUBLIC_WS
from .demo_paper.runtime import Session, install_stop_signals
from .demo_paper.transport import DemoRest, DemoReject, private_stream

DURATION = 86400
PROFILE = "trading-24h-v1"
STRATEGIES = ["level_breakout", "weak_level_rejection", "trend_structure"]


def settings(output, *, equity=1000.0):
    values = {k: f.default for k, f in Settings.model_fields.items()}
    values.update(start_balance=1000.0, paper_run_duration_seconds=DURATION,
        run_label=PROFILE, session_dir=str(output), trading_quality_enabled=True,
        bybit_rest_url=PUBLIC_REST, bybit_rest_fallback_urls="", bybit_public_ws_url=PUBLIC_WS,
        bybit_api_key=SecretStr(""), bybit_api_secret=SecretStr(""), fee_rate_mode="configured",
        exchange_clock_enabled=True, confirmed_candle_stale_seconds=150,
        breakout_enabled=True, weak_level_rejection_enabled=True, trend_structure_enabled=True,
        density_enabled=True, price_action_hypothesis_enabled=False, staged_entries_enabled=False,
        enforce_session_loss_limit=False, enforce_strategy_expectancy_gate=False,
        segment_expectancy_mode="off", research_policy_mode="off", research_policy_file="",
        enforce_net_reward_risk_gate=True, min_net_reward_risk=1.15,
        absolute_min_net_reward_risk=1.0, enforce_winner_cost_share_gate=True,
        enforce_min_net_profit_gate=False, e01_breakout_obstacle_veto=False,
        research_frame_seconds=5, replay_idle_frame_seconds=15,
        research_trade_delta_enabled=True, replay_trade_delta_enabled=True)
    if not 0 < equity < float("inf"):
        raise SafetyError("positive finite Demo USDT equity required")
    # Same dollar risk as the 1000-USDT paper profile when equity is larger;
    # proportionally less when smaller. Never pretend wallet equity is 1000.
    scale = min(1000.0, equity) / equity
    values["start_balance"] = equity
    for field in ("risk_fraction", "max_trade_all_in_loss_fraction", "max_total_risk_fraction",
                  "max_leverage", "max_position_leverage"):
        values[field] *= scale
    return Settings(_env_file=None, **values)


def manifest(config):
    from .run_manifest import build_run_manifest, code_provenance
    result = build_run_manifest(config, {k: True for k in STRATEGIES},
                               code=code_provenance(Path(__file__).resolve().parents[1]), policy={"mode": "off"})
    result.update(profile=PROFILE, durationSeconds=DURATION, startBalance=config.start_balance, referenceBalance=1000,
        tradeableStrategies=STRATEGIES, evidenceOnly=["orderbook_density"],
        disabledStrategies={"price_action_hypothesis": "opt-in paper beta, not qualified for Demo"},
        mlAuthority=dict(entries=False, veto=False, rank=False, risk=False, size=False, worker=False),
        crossVenue="deterministic_entry_context", demoEquityPolicy="actual USDT equity; risk/exposure budget capped to min(actual,1000)",
        exitPlanPolicy="prefer qualified partial; otherwise qualified full closure at the same reachable target",
        artificialLimits=dict(sessionLoss=False, dailyLoss=False, drawdownKill=False,
                              tradeCount=False, expectancy=False, segmentAdaptive=False),
        policyNotes="1.15 RR, participation and reaction thresholds are conservative policy values, not optima")
    from .manifest_validation import fingerprint
    result["manifestSha256"] = fingerprint({k:v for k,v in result.items() if k not in {"manifestSha256", "manifestId"}})
    return result


class CrossContext:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cross = CrossVenueRuntime(self.recorder.path.name)
        owner = self
        class Journal:
            def append(self, kind, payload):
                # Store classification decisions and faults; avoid duplicating every raw external tick for 24h.
                if kind in {"venue_error", "venue_gap"}:
                    owner.recorder.record("cross_" + kind, payload.get("symbol"), payload)
        self.cross_service = PublicCrossVenueService(self.cross, Journal())
        self.cross_task = None

    def _market_handler(self, symbol):
        handler, fast, deep = super()._market_handler(symbol)
        depths = {f"orderbook.{self.config.fast_orderbook_depth}.": self.config.book_stale_seconds,
                  f"orderbook.{self.config.deep_orderbook_depth}.": self.config.deep_book_stale_seconds}

        async def current_message(message):
            # recv() freshness cannot detect frames buffered upstream of our
            # application queue. Recover the affected socket with a new snapshot;
            # never skip individual orderbook deltas and keep using that book.
            topic = str(message.get("topic") or "")
            limit = next((v for prefix, v in depths.items() if topic.startswith(prefix)), None)
            stamp = message.get("cts") or message.get("ts")
            clock = self._clock_state() if limit is not None and stamp else None
            if clock and clock["valid"]:
                age = clock["exchange_lower_ms"] - float(stamp)
                if age > 2 * limit * 1000:
                    from .bybit import MarketDataBackpressureError
                    self.recorder.record("market_backlog_reconnect", symbol,
                        dict(topic=topic, exchangeAgeMs=age, entryLimitMs=limit * 1000,
                             recoveryLimitMs=2 * limit * 1000))
                    raise MarketDataBackpressureError(f"upstream {topic} backlog: {age:.0f}ms")
            await handler(message)

        return current_message, fast, deep

    async def start(self):
        self.cross_task = asyncio.create_task(self._cross_loop(), name="trading-cross-venue")
        await super().start()

    async def _cross_loop(self):
        refresh = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= refresh:
                await self.cross_service.start()
                refresh = time.monotonic() + 300
            self.cross_service.watch(list(self.sessions))
            await asyncio.sleep(1)

    async def close(self):
        if self.cross_task is not None:
            self.cross_task.cancel()
            await asyncio.gather(self.cross_task, return_exceptions=True)
        await self.cross_service.close()
        await super().close()


class PaperEngine(CrossContext, TradingEngine):
    pass


class DemoEngine(CrossContext, PairedEngine):
    """Same scanner/strategy/admission as Paper; two independent existing ledgers."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paired_book_cache = {}

    def _invalidate_transport(self, symbol, event, fast_state, deep_state):
        TradingEngine._invalidate_transport(self, symbol, event, fast_state, deep_state)
        self._paired_book_cache.pop(symbol, None)
        self.portfolio.books.pop(symbol, None)
        self.portfolio.arms["paper"].venue.books.pop(symbol, None)
        pair = self.portfolio.by_symbol.get(symbol)
        if pair:
            self.portfolio.cancel_entries.add(pair)

    def _submit_research_opportunity(self, best):
        # Idle symbols need no execution-depth merge on every quote. Prime both
        # ledgers synchronously before reservation/dispatch using current depth.
        if not execution_books_ready(self, best.session):
            self._risk_reject_if_changed(best.session, best.decision, "paired_execution_book_unhealthy")
            return True
        self._market_for_pair(best.session, force=True)
        return super()._submit_research_opportunity(best)

    def _market_for_pair(self, session, *, force=False, **kwargs):
        if not force and session.symbol not in self.portfolio.by_symbol:
            self._paired_book_cache.pop(session.symbol, None)
            self.portfolio.books.pop(session.symbol, None)
            self.portfolio.arms["paper"].venue.books.pop(session.symbol, None)
            return
        if not execution_books_ready(self, session):
            self.portfolio.books.pop(session.symbol, None)
            self.portfolio.arms["paper"].venue.books.pop(session.symbol, None)
            return
        super()._market_for_pair(session, **kwargs)

    def _paired_execution_book(self, session):
        from .execution_book import coherent_execution_book
        fast = session.orderbook
        deep = session.depth_orderbook() if session.deep_book_is_fresh() else None
        cached = self._paired_book_cache.get(session.symbol)
        if cached is None or cached[0] is not fast or cached[1] is not deep:
            cached = (fast, deep, coherent_execution_book(fast, deep))
            if session.symbol not in self._paired_book_cache and len(self._paired_book_cache) >= max(1, self.config.max_active_symbols):
                self._paired_book_cache.pop(next(iter(self._paired_book_cache)))
            self._paired_book_cache[session.symbol] = cached
        # OrderBookState publishes new objects on each update. All individual
        # trades still visit PaperVenue in causal order for maker confirmation.
        return cached[2]

    def _cancel_all_pending(self, reason):
        if reason == "clock_invalid":
            self.portfolio.accepting = False
            self.portfolio.cancel_entries.update(self.portfolio.reservations)
            return
        super()._cancel_all_pending(reason)

    def _validate_pending_entry(self, session):
        pair = self.portfolio.by_symbol.get(session.symbol)
        if pair and self._clock_entry_block(session):
            self.portfolio.cancel_entries.add(pair)
            return
        super()._validate_pending_entry(session)


def execution_books_ready(engine, session):
    if not session.book_is_fresh() or not session.deep_book_is_fresh():
        return False
    clock = engine._clock_state()
    if clock and clock["valid"]:
        for stamp, limit in ((session.fast_book_exchange_ts_ms, session.book_stale_after_seconds),
                             (session.deep_book_exchange_ts_ms, session.deep_book_stale_after_seconds)):
            if stamp <= 0 or clock["exchange_lower_ms"] - stamp > limit * 1000:
                return False
        # The old generic skew helper only checked deep lagging behind fast.
        # A delayed fast feed must not combine with a far newer deep quote.
        if abs(session.fast_book_exchange_ts_ms - session.deep_book_exchange_ts_ms) > session.deep_book_max_skew_seconds * 1000:
            return False
    return True


class ReadRetryDemoRest(DemoRest):
    async def request(self, method, path, params=None):
        for attempt in range(4):
            try:
                return await super().request(method, path, params)
            except SafetyError as exc:
                transient = (isinstance(exc, DemoReject) and exc.code in {10000, 10002, 10006, 10016, 10019}
                    or str(exc) in {"Demo transport outcome unknown", "Demo HTTP status 429", "Demo HTTP status 502", "Demo HTTP status 503", "Demo HTTP status 504"})
                # Mutations are never retried: resolve uncertain outcomes by owned orderLinkId.
                if method != "GET" or not transient or attempt == 3:
                    raise
                await asyncio.sleep(min(2 ** attempt, 4))


def require_demo_equity(connected):
    rows = connected.get("wallet", {}).get("list", [])
    if len(rows) != 1:
        raise SafetyError("Demo wallet/equity unavailable")
    coins = rows[0].get("coin", [])
    from .demo_paper.contracts import dec
    usdt = next((dec(c["equity"]) for c in coins if c.get("coin") == "USDT"), None)
    if usdt is None:
        raise SafetyError("Demo USDT equity unavailable")
    if usdt <= 0 or not usdt.is_finite():
        raise SafetyError("positive finite Demo USDT equity required; no orders sent")
    return float(usdt)


async def no_order_preflight(config, credentials, *, seconds=35):
    """Read-only connection, real heartbeat, public scanner/book/depth and clock."""
    rest = ReadRetryDemoRest(credentials, enabled=True)
    public = PublicRest(config)
    stop = asyncio.Event()
    ready = asyncio.Event()
    heartbeat = asyncio.Event()
    async def message(row):
        # Recheck account after private subscription; foreign work is never cancelled.
        pass
    async def gap(reason):
        if reason == "reconnected": ready.set()
        else:
            ready.clear()
            heartbeat.clear()
    def diagnostic(event, row):
        if event == "private_heartbeat" and row.get("pong", 0) > 0: heartbeat.set()
    engine = PaperEngine(config, rest_client=public, configure_observability=False)
    task = None
    try:
        connected = await connected_preflight(rest, public, credentials)
        equity = require_demo_equity(connected)
        task = asyncio.create_task(private_stream(credentials, message, gap, stop, diagnostics=diagnostic))
        await engine.start()
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if ready.is_set() and heartbeat.is_set() and engine.start_block_reason() is None:
                final = await connected_preflight(rest, public, credentials)
                require_demo_equity(final)
                return dict(status="CONNECTED_NO_ORDER_PASS", actualEquityUSDT=equity, ordersSent=0,
                    privateHeartbeat=True, clock=engine._clock_state(), market=engine.market_health(),
                    instruments={s: x.instrument.public() for s, x in engine.sessions.items()})
            await asyncio.sleep(.25)
        raise SafetyError("no-order preflight incomplete: private heartbeat/public L50+L1000/clock not ready; no orders sent")
    finally:
        stop.set()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.close()
        await rest.close()


class DemoSession(Session):
    def __init__(self, config, credentials, output):
        super().__init__(config, None, None, credentials, {}, output, rule_only=True, engine_class=DemoEngine)
        self.events = deque(maxlen=1000)
        self.recovery = {}
        self.reconcile_lock = asyncio.Lock()
        self.connected = None
        self.terminal_halt = self.portfolio.halt
        self.portfolio.halt = self.halt
        self.engine.cross_demo_session = self

    def halt(self, reason):
        if reason in {"execution_failure", "management_failure", "protection_failure", "order_presence_unknown"}:
            self.portfolio.accepting = False
            self.recovery.setdefault("execution", time.monotonic())
            self.portfolio.cancel_entries.update(self.portfolio.reservations)
            return
        self.terminal_halt(reason)

    def before_write(self, path, params):
        if path == "/v5/order/create" and not params.get("reduceOnly") and self.start_ns is not None and duration_elapsed(self.start_ns):
            raise SafetyError("24h deadline reached before entry send")
        super().before_write(path, params)

    async def manage(self, name):
        while not self.finished:
            try:
                await self.portfolio.manage_arm(name)
            except Exception as exc:
                self.recovery.setdefault("execution", time.monotonic())
                self.portfolio.accepting = False
                self.emit("management_recovery", dict(arm=name, error_type=type(exc).__name__))
                if isinstance(exc, SafetyError) and any(word in str(exc) for word in ("foreign", "mismatch", "exceeds", "ownership")):
                    self.terminal_halt("corrupted_execution_state")
            await asyncio.sleep(.05)

    async def initialize_rest(self):
        await self.rest.close()
        self.rest = ReadRetryDemoRest(self.credentials, enabled=True)
        self.rest.before_write = self.before_write
        self.arms["demo"].venue.rest = self.rest

    async def reconcile(self, *, final=False):
        async with self.reconcile_lock:
            await super().reconcile(final=final)

    async def gap(self, reason):
        if reason == "reconnected":
            await self.reconcile()
            self.private_ready.set()
            self.recovery.pop("private", None)
        else:
            self.private_ready.clear()
            self.recovery.setdefault("private", time.monotonic())
            self.portfolio.accepting = False
            self.portfolio.cancel_entries.update(self.portfolio.reservations)
            self.emit("private_gap", dict(reason=reason))

    async def account_loop(self):
        while not self.finished:
            try:
                await self.reconcile()
                await self.funding()
                self.recovery.pop("account", None)
                venue = self.arms["demo"].venue
                if all(o.confirmed and not o.unknown and o.fees_known for o in venue.orders.values()):
                    self.recovery.pop("execution", None)
                    if venue.failure in {"order_presence_unknown", "SafetyError", "DemoReject"}:
                        venue.healthy = True
                        venue.failure = None
            except Exception as exc:
                self.portfolio.accepting = False
                self.recovery.setdefault("account", time.monotonic())
                self.emit("reconcile_failure", dict(error_type=type(exc).__name__))
                if (isinstance(exc, DemoReject) and exc.code in {10003,10004,10005,10007,33004}
                    or isinstance(exc, SafetyError) and any(word in str(exc) for word in ("foreign", "mismatch", "hedge", "identity", "permission"))):
                    self.portfolio.halt("unrecoverable_account_state")
            await asyncio.sleep(2)

    def refresh_admission(self):
        clock = self.engine._clock_state()
        healthy = self.private_ready.is_set() and not self.recovery and (clock is None or clock["valid"])
        self.portfolio.accepting = healthy and not self.stop.is_set() and self.portfolio.stop_reason is None

    def check_exposure_health(self):
        now = time.monotonic()
        for symbol, pair in list(self.portfolio.by_symbol.items()):
            session = self.engine.sessions.get(symbol)
            key = "market:" + symbol
            healthy = session is not None and execution_books_ready(self.engine, session)
            if healthy:
                self.recovery.pop(key, None)
            else:
                self.portfolio.books.pop(symbol, None)
                self.arms["paper"].venue.books.pop(symbol, None)
                self.recovery.setdefault(key, now)
                self.portfolio.cancel_entries.add(pair)
        for key in list(self.recovery):
            if key.startswith("market:") and key[7:] not in self.portfolio.by_symbol:
                self.recovery.pop(key)
        if self.portfolio.has_execution_work and any(now - since >= 20 for since in self.recovery.values()):
            # Bybit-owned reduce-only emergency liquidation, not external-venue authority.
            for pair in self.portfolio.reservations:
                for name in self.arms:
                    self.portfolio.exit_reasons[name, pair] = "recovery_protection"
        if self.portfolio.has_execution_work and any(now - since >= 90 for since in self.recovery.values()):
            self.portfolio.halt("unable_to_protect_after_bounded_recovery")

    async def run_session(self):
        restore = install_stop_signals(self.stop)
        reason = "preflight_failed"
        complete = False
        try:
            await self.initialize_rest()
            self.connected = await connected_preflight(self.rest, self.public, self.credentials)
            actual = require_demo_equity(self.connected)
            if abs(actual - self.config.start_balance) > .01:
                raise SafetyError("Demo equity changed since preflight; rerun to freeze correct risk scaling")
            self.emit("connected_preflight", self.connected)
            self.tasks.append(asyncio.create_task(private_stream(self.credentials, self.private_message, self.gap, self.stop, diagnostics=self.emit)))
            await asyncio.wait_for(self.private_ready.wait(), 35)
            self.start_ns = time.perf_counter_ns()
            self.start_ms = int(time.time() * 1000)
            self.portfolio.started_ns = self.start_ns
            self.rest.write_enabled = True
            self.tasks.extend(asyncio.create_task(self.portfolio.dispatch(n)) for n in self.arms)
            self.tasks.extend(asyncio.create_task(self.manage(n)) for n in self.arms)
            self.account_task = asyncio.create_task(self.account_loop())
            self.tasks.append(self.account_task)
            startup = asyncio.create_task(self.engine.start())
            self.tasks.append(startup)
            self.engine.running = True
            while not self.stop.is_set() and not self.portfolio.stop_reason and not duration_elapsed(self.start_ns):
                self.check_exposure_health()
                self.refresh_admission()
                if startup.done() and startup.exception():
                    raise startup.exception()
                check_runtime_tasks(self.engine)
                if (self.output / "STOP").exists(): self.stop.set()
                write_status(self.output, self.engine, mode="demo", recovery=self.recovery,
                             started_ns=self.start_ns, actual_equity=self.config.start_balance)
                await asyncio.sleep(.25)
            reason = self.portfolio.stop_reason or ("operator_stop" if self.stop.is_set() else "duration_elapsed")
        except Exception as exc:
            reason = "failure_" + type(exc).__name__
            self.emit("run_failure", dict(error_type=type(exc).__name__))
        finally:
            self.engine.running = False
            self.terminal_halt(reason)
            if self.account_task:
                self.account_task.cancel()
                await asyncio.gather(self.account_task, return_exceptions=True)
            until = time.monotonic() + 90
            stable = 0
            while self.start_ns and time.monotonic() < until:
                try:
                    await asyncio.wait_for(self.reconcile(final=True), 15)
                    if not self.portfolio.has_execution_work:
                        stable += 1
                        if stable >= 2:
                            await asyncio.wait_for(self.funding(), 10)
                            if self.funding_complete():
                                complete = True
                                break
                except Exception as exc:
                    stable = 0
                    self.emit("shutdown_reconcile_failure", dict(error_type=type(exc).__name__))
                await asyncio.sleep(1)
            self.finished = True
            self.stop.set()
            for task in self.tasks: task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            try:
                await self.engine.close()
            finally:
                await self.rest.close()
                restore()
            result = dict(reason=reason, finalized=complete, mode="demo", actualEquityAtStart=self.config.start_balance if self.connected else None,
                          fundingComplete=self.funding_complete(),
                          balances={n: a.broker.balance for n, a in self.arms.items()},
                          trades={n: list(a.broker.closed_trades) for n, a in self.arms.items()})
            save_json(self.output / "summary.json", result)
        return result


def duration_elapsed(started_ns, now_ns=None):
    return (time.perf_counter_ns() if now_ns is None else now_ns) - started_ns >= DURATION * 1_000_000_000


def check_runtime_tasks(engine):
    for task in engine._tasks:
        if task.done():
            raise SafetyError("ordinary runtime service stopped unexpectedly: " + task.get_name())
    if engine.recorder.health().get("writerError"):
        raise SafetyError("session persistence failed")


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                if path.name == "status.json": return  # A brief dashboard read must not stop trading.
                raise
            time.sleep(.01)


def write_status(output, engine, *, mode, recovery=None, started_ns=None, actual_equity=None):
    save_json(output / "status.json", dict(mode=mode, running=engine.running,
        elapsedSeconds=(time.perf_counter_ns() - started_ns) / 1e9 if started_ns else 0,
        durationSeconds=DURATION, balance=engine.broker.balance, actualDemoEquityAtStart=actual_equity,
        positions={s: p.public() for s, p in engine.broker.positions.items()}, strategies=engine.strategy_enabled,
        market=engine.market_health(), clock=engine._clock_state(), recovery=list(recovery or {}),
        mlAuthority="OFF", session=str(engine.recorder.path), updatedAt=time.time()))


async def run_paper(config, output, *, smoke_seconds=None, engine=None):
    engine = engine or PaperEngine(config, rest_client=PublicRest(config), configure_observability=False)
    stop = asyncio.Event()
    restore = install_stop_signals(stop)
    started = time.perf_counter_ns()
    startup = asyncio.create_task(engine.start())
    reason = "operator_stop"
    ready_seen = False
    engine.running = True
    # The coordinator owns the sole monotonic deadline, including bootstrap and pauses.
    engine._run_started_at = time.time()
    engine._run_started_mono = started / 1e9
    engine._run_manifest = engine._build_trading_manifest()
    try:
        while not stop.is_set() and not duration_elapsed(started):
            if startup.done() and startup.exception(): raise startup.exception()
            check_runtime_tasks(engine)
            ready_seen = ready_seen or engine.start_block_reason() is None
            if (output / "STOP").exists(): break
            if smoke_seconds and (time.perf_counter_ns() - started) / 1e9 >= smoke_seconds:
                reason = "smoke_complete"
                break
            write_status(output, engine, mode="paper", started_ns=started)
            await asyncio.sleep(.25)
        if duration_elapsed(started): reason = "duration_elapsed"
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        engine._stop_trading(reason)
        await engine.close()
        restore()
        result = dict(reason=reason, finalized=not engine.broker.positions and not engine.broker.pending_entries,
            readySeen=ready_seen, balance=engine.broker.balance, closedTrades=list(engine.broker.closed_trades),
            market=engine.market_health(), session=str(engine.recorder.path))
        save_json(output / "summary.json", result)
    if smoke_seconds and not ready_seen: raise SafetyError("Paper smoke never reached healthy public startup")
    return result


def dashboard_state(engine, output, mode, symbol=None, session=None):
    state = engine.public_state(symbol)
    started = session.start_ns if session is not None else (
        int(engine._run_started_mono * 1e9) if engine._run_started_mono is not None else None)
    elapsed = max(0, (time.perf_counter_ns() - started) / 1e9) if started else 0
    state["mode"] = mode
    state["run"].update(configuredDurationSeconds=DURATION, elapsedSeconds=elapsed,
        remainingSeconds=max(0, DURATION - elapsed),
        deadlineAt=(session.start_ms / 1000 + DURATION if session and session.start_ms else
                    engine._run_started_at + DURATION if engine._run_started_at else None))
    stopping = (output / "STOP").exists() or bool(session and session.stop.is_set())
    state["controls"] = dict(startAllowed=False, strategiesLocked=True, stopRequested=stopping)
    state["trading24h"] = dict(profile=PROFILE, mlAuthority="OFF", referenceBalance=1000,
        actualEquityAtStart=engine.config.start_balance, recovery=list(session.recovery) if session else [],
        message=("Штатное завершение: сверка и закрытие позиций…" if stopping else
                 f"{PROFILE} · ML OFF · 24 часа · reference 1000 USDT"))
    if session is not None:
        # The portfolio combines ownership for admission. The UI must show actual
        # Demo facts, not the minimum balance or merged positions of both ledgers.
        broker = session.arms["demo"].broker
        state.update(balance=broker.balance, totalPnl=broker.total_pnl,
            positions=[p.public() for p in broker.positions.values()],
            closedTrades=list(broker.closed_trades[-30:]))
        for row in state["working"]:
            position = broker.positions.get(row["symbol"])
            row["position"] = position.public() if position else None
        state["portfolio"].update(totalExposure=broker.total_exposure,
            grossLeverage=broker.total_exposure / broker.balance if broker.balance else 0)
        state["trading24h"]["comparisonPaper"] = dict(balance=session.arms["paper"].broker.balance,
            positions=len(session.arms["paper"].broker.positions))
    return state


def create_dashboard(engine, output, mode, session=None):
    from fastapi import FastAPI, HTTPException
    from .ui import mount_trading_ui
    app = FastAPI(title="Scalp Bot")

    async def state(symbol=None):
        return dashboard_state(engine, output, mode, symbol, session)

    mount_trading_ui(app, lambda: engine, state)

    @app.post("/api/bot/start")
    async def start_bot():
        raise HTTPException(409, "24h run запускается launcher; UI не сбрасывает его deadline")

    @app.post("/api/strategies/{key}")
    async def toggle_strategy(key: str):
        raise HTTPException(409, "Состав стратегий зафиксирован профилем trading-24h-v1")

    @app.post("/api/bot/stop")
    async def stop_bot():
        (output / "STOP").touch()
        engine.running = False
        if session is not None:
            session.portfolio.accepting = False
            session.stop.set()
        return dict(ok=True, running=False, finalizing=True)

    return app


async def dashboard(app, listener, stop):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    # The coordinator's handlers own graceful order finalization.
    from contextlib import nullcontext
    server.capture_signals = nullcontext
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await stop.wait()
    finally:
        server.should_exit = True
        await task


async def main_async(args):
    output = Path(args.output).resolve()
    config = settings(output / "sessions")
    if args.action == "check":
        print(json.dumps(manifest(config), ensure_ascii=False, indent=2))
        return
    if args.action == "stop":
        if not output.is_dir(): raise SafetyError("run directory does not exist")
        (output / "STOP").touch()
        print("Graceful stop requested; wait for summary.json finalized=true")
        return
    if args.action == "connected-check":
        credentials = load_credentials(args.credentials)
        result = await no_order_preflight(settings(output.parent / (output.name + "-preflight")), credentials)
        print(json.dumps(result, indent=2, default=str))
        return
    if output.exists(): raise SafetyError("use a new output directory for each start")
    # Reserve the UI port before creating a runtime or enabling any Demo writes.
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
            listener.setblocking(False)
        except OSError as exc:
            raise SafetyError(f"UI port {args.port} unavailable; trading was not started") from exc
        await start_run(args, output, config, listener)


async def start_run(args, output, config, listener):
    credentials = load_credentials(args.credentials) if args.mode == "demo" else None
    if args.mode == "demo":
        # Runs with write_enabled=False before EVERY Demo start.
        preflight_config = settings(output.parent / (output.name + "-preflight"))
        result = await no_order_preflight(preflight_config, credentials)
        print(json.dumps(result, indent=2, default=str))
        config = settings(output / "sessions", equity=result["actualEquityUSDT"])
    if args.mode == "demo":
        session = DemoSession(config, credentials, output)
        engine = session.engine
    else:
        output.mkdir(parents=True)
        session = None
        engine = PaperEngine(config, rest_client=PublicRest(config), configure_observability=False)
    save_json(output / "profile.json", manifest(config))
    stop_ui = asyncio.Event()
    ui = asyncio.create_task(dashboard(create_dashboard(engine, output, args.mode, session), listener, stop_ui))
    print(f"UI http://127.0.0.1:{args.port}/ · results {output}", flush=True)
    try:
        result = await session.run_session() if args.mode == "demo" else await run_paper(config, output, smoke_seconds=args.smoke_seconds, engine=engine)
        print(json.dumps(result, default=str), flush=True)
        if not result["finalized"]: raise SafetyError("finalization incomplete; inspect owned Demo exposure and retained evidence")
        if result["reason"] not in {"operator_stop", "duration_elapsed", "smoke_complete"}:
            raise SafetyError("trading runtime stopped: " + result["reason"])
    finally:
        stop_ui.set()
        await ui


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "connected-check", "start", "stop"])
    parser.add_argument("--mode", choices=["paper", "demo"], default="paper")
    parser.add_argument("--output", default="data/trading-24h-v1/run")
    parser.add_argument("--credentials", default=".env.demo-paper.local")
    parser.add_argument("--port", type=int)
    parser.add_argument("--smoke-seconds", type=int)
    args = parser.parse_args()
    if args.port is None: args.port = 8000 if args.mode == "paper" else 8011
    if args.smoke_seconds and args.mode != "paper": parser.error("smoke is public Paper only")
    if args.action == "connected-check" and args.mode != "demo": parser.error("connected-check requires demo")
    try:
        asyncio.run(main_async(args))
    except SafetyError as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
