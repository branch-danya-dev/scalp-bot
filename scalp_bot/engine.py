from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from time import monotonic, time

from .bybit import BybitRestClient, OrderBookState, stream_symbol
from .config import Settings
from .domain import Candle, Candidate, OrderBook, StrategyDecision, Trend
from .paper import PaperBroker
from .recorder import SessionRecorder
from .risk import RiskEngine
from .strategies import DEFAULT_STRATEGIES, Strategy, classify_trend


@dataclass(slots=True)
class SymbolState:
    symbol: str
    candles: list[Candle] = field(default_factory=list)
    context_15m: list[Candle] = field(default_factory=list)
    orderbook: OrderBook = field(default_factory=OrderBook)
    last_price: float = 0.0
    trend: Trend = Trend.FLAT
    decisions: dict[str, StrategyDecision] = field(default_factory=dict)
    last_eval: float = 0.0

    def market_snapshot(self) -> dict:
        return {
            "symbol": self.symbol,
            "lastPrice": self.last_price,
            "trend": self.trend.value,
            "candles": [x.public() for x in self.candles[-240:]],
            "orderbook": self.orderbook.public(),
            "decisions": {k: v.public() for k, v in self.decisions.items()},
        }


class TradingEngine:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.rest = BybitRestClient(config)
        self.risk = RiskEngine(config)
        self.broker = PaperBroker(config)
        self.recorder = SessionRecorder(config.session_dir)
        self.strategies: dict[str, Strategy] = {x.key: x for x in DEFAULT_STRATEGIES}
        self.strategy_enabled: dict[str, bool] = {x.key: True for x in DEFAULT_STRATEGIES}
        self.running = False
        self.candidates: list[Candidate] = []
        self.states: dict[str, SymbolState] = {}
        self.events: deque[dict] = deque(maxlen=160)
        self._tasks: list[asyncio.Task] = []
        self._worker_tasks: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self._stop = asyncio.Event()
        self._decision_fingerprints: dict[tuple[str, str], tuple] = {}

    async def start(self) -> None:
        self._stop.clear()
        await self._scan_once()
        self._tasks = [
            asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._context_loop(), name="context"),
        ]

    async def close(self) -> None:
        self._stop.set()
        for task, stop_event in self._worker_tasks.values():
            stop_event.set()
            task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*(x[0] for x in self._worker_tasks.values()), *self._tasks, return_exceptions=True)
        await self.rest.close()

    def set_running(self, value: bool) -> None:
        self.running = value
        self._emit("bot_started" if value else "bot_stopped", None, {})

    def toggle_strategy(self, key: str, enabled: bool) -> None:
        if key not in self.strategy_enabled:
            raise KeyError(key)
        self.strategy_enabled[key] = enabled
        self._emit("strategy_toggle", None, {"strategy": key, "enabled": enabled})

    async def _scanner_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.config.scanner_interval_seconds)
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("scanner_error", None, {"error": str(exc)})

    async def _scan_once(self) -> None:
        self.candidates = await self.rest.candidates(limit=12)
        desired = [x.symbol for x in self.candidates[: self.config.working_symbols]]
        if self.broker.position and self.broker.position.symbol not in desired:
            desired.append(self.broker.position.symbol)
        await self._sync_workers(desired)
        self._emit("scanner_update", None, {"working": desired, "candidates": [x.symbol for x in self.candidates]})

    async def _sync_workers(self, desired: list[str]) -> None:
        desired_set = set(desired)
        for symbol in list(self._worker_tasks):
            if symbol not in desired_set:
                task, stop_event = self._worker_tasks.pop(symbol)
                stop_event.set()
                task.cancel()
                self.states.pop(symbol, None)
        for symbol in desired:
            if symbol in self._worker_tasks:
                continue
            await self._bootstrap_symbol(symbol)
            stop_event = asyncio.Event()
            task = asyncio.create_task(self._symbol_worker(symbol, stop_event), name=f"market-{symbol}")
            self._worker_tasks[symbol] = (task, stop_event)

    async def _bootstrap_symbol(self, symbol: str) -> None:
        candles, context = await asyncio.gather(
            self.rest.klines(symbol, "1", 240),
            self.rest.klines(symbol, "15", 120),
        )
        state = SymbolState(symbol=symbol, candles=candles, context_15m=context)
        state.last_price = candles[-1].close if candles else 0
        state.trend = classify_trend(context)
        self.states[symbol] = state

    async def _context_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60)
                for symbol, state in list(self.states.items()):
                    state.context_15m = await self.rest.klines(symbol, "15", 120)
                    state.trend = classify_trend(state.context_15m)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("context_error", None, {"error": str(exc)})

    async def _symbol_worker(self, symbol: str, stop_event: asyncio.Event) -> None:
        book_state = OrderBookState()

        async def on_message(message: dict) -> None:
            state = self.states.get(symbol)
            if state is None:
                return
            topic = message.get("topic", "")
            if topic.startswith("orderbook."):
                state.orderbook = book_state.apply(message)
            elif topic.startswith("kline."):
                self._apply_kline(state, message)
            elif topic.startswith("publicTrade."):
                rows = message.get("data") or []
                if rows:
                    state.last_price = float(rows[-1]["p"])
                    trade = self.broker.mark(symbol, state.last_price, state.orderbook.spread_pct)
                    if trade:
                        self._emit("trade_closed", symbol, trade, snapshot=True)
            now = monotonic()
            if now - state.last_eval >= 0.8:
                state.last_eval = now
                await self._evaluate(state)

        await stream_symbol(self.config.bybit_public_ws_url, symbol, on_message, stop_event)

    @staticmethod
    def _apply_kline(state: SymbolState, message: dict) -> None:
        rows = message.get("data") or []
        if not rows:
            return
        row = rows[-1]
        candle = Candle(
            start_ms=int(row["start"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            turnover=float(row["turnover"]),
            confirmed=bool(row.get("confirm")),
        )
        state.last_price = candle.close
        if state.candles and state.candles[-1].start_ms == candle.start_ms:
            state.candles[-1] = candle
        else:
            state.candles.append(candle)
            state.candles = state.candles[-240:]

    async def _evaluate(self, state: SymbolState) -> None:
        if not state.candles:
            return
        state.trend = classify_trend(state.context_15m)
        trade_candidates: list[StrategyDecision] = []
        for key, strategy in self.strategies.items():
            if not self.strategy_enabled[key]:
                continue
            decision = strategy.evaluate(state.candles, state.orderbook, state.trend)
            state.decisions[key] = decision
            self._record_decision_if_changed(state, decision)
            if decision.tradeable:
                trade_candidates.append(decision)

        if not self.running or self.broker.position is not None or not trade_candidates:
            return
        decision = max(trade_candidates, key=lambda x: x.confidence)
        result = self.risk.build_plan(
            state.symbol,
            decision,
            self.broker.balance,
            state.orderbook.spread_pct,
        )
        if not result.allowed or result.plan is None:
            self._emit(
                "risk_reject",
                state.symbol,
                {"strategy": decision.strategy, "reason": result.reason, "decision": decision.public()},
                snapshot=True,
            )
            return
        position = self.broker.open(result.plan, state.orderbook.spread_pct)
        self._emit(
            "trade_opened",
            state.symbol,
            {"plan": result.plan.public(), "position": position.public(), "reasons": decision.reasons},
            snapshot=True,
        )

    def _record_decision_if_changed(self, state: SymbolState, decision: StrategyDecision) -> None:
        fingerprint = (
            decision.action.value,
            round(decision.watched_level or 0, 8),
            tuple(decision.reasons),
        )
        key = (state.symbol, decision.strategy)
        if self._decision_fingerprints.get(key) == fingerprint:
            return
        self._decision_fingerprints[key] = fingerprint
        self._emit("decision", state.symbol, decision.public())

    def _emit(self, event: str, symbol: str | None, payload: dict, snapshot: bool = False) -> None:
        row = {"ts": time(), "event": event, "symbol": symbol, "payload": payload}
        self.events.appendleft(row)
        stored = dict(payload)
        if snapshot and symbol in self.states:
            stored["market"] = self.states[symbol].market_snapshot()
        self.recorder.record(event, symbol, stored)

    def public_state(self, selected_symbol: str | None = None) -> dict:
        working = list(self.states)
        if selected_symbol not in self.states:
            selected_symbol = working[0] if working else None
        market = self.states[selected_symbol].market_snapshot() if selected_symbol else None
        candidate_map = {x.symbol: x for x in self.candidates}
        working_rows = []
        for symbol in working:
            candidate = candidate_map.get(symbol)
            state = self.states[symbol]
            working_rows.append(
                {
                    "symbol": symbol,
                    "turnover24h": candidate.turnover_24h if candidate else None,
                    "change24h": candidate.change_24h if candidate else None,
                    "lastPrice": state.last_price,
                    "trend": state.trend.value,
                }
            )
        return {
            "botRunning": self.running,
            "mode": "paper",
            "balance": self.broker.balance,
            "totalPnl": self.broker.total_pnl,
            "position": self.broker.position.public() if self.broker.position else None,
            "closedTrades": self.broker.closed_trades[-20:],
            "working": working_rows,
            "candidates": [x.public() for x in self.candidates],
            "market": market,
            "events": list(self.events)[:60],
            "strategies": [
                {"key": key, "label": strategy.label, "enabled": self.strategy_enabled[key]}
                for key, strategy in self.strategies.items()
            ],
            "risk": {
                "minNetProfitUsd": self.config.min_net_profit_usd,
                "riskFraction": self.config.risk_fraction,
                "maxLeverage": self.config.max_leverage,
                "takerFeeRate": self.config.taker_fee_rate,
                "slippageBps": self.config.slippage_bps,
            },
            "sessionFile": str(self.recorder.path),
        }
