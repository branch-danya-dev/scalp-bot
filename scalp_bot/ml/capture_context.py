"""Causal saved-capture adapter. No engine instance, exchange client or orders.

Uses the actual book reconstructor, session candle/OFI helpers, common flow and
forming computations, and M1b1 feature adapter. Unsupported groups remain None.
"""
from collections import deque
from types import SimpleNamespace

from ..bybit import OrderBookState, OrderBookSequenceError
from ..domain import Candle, OrderBook, TradeTick, Trend
from ..engine import ActiveSymbolSession, TradingEngine
from ..instrument import InstrumentSpec
from ..strategy.common import compute_trade_flow
from ..strategy.flow import best_level_ofi_usd, prune_trades
from ..strategy.flow_context import build_multi_horizon_flow_context
from ..strategy.market_context import MarketContext, build_execution_context
from ..strategy.pre_state import build_forming_candle_context
from .features import ContextCoverage, FEATURE_SCHEMA, extract_context_features
from .contracts import SnapshotRef


class CaptureContext:
    def __init__(self, symbol, capture_id, bootstrap, mono_ns):
        self.symbol, self.capture_id = symbol, capture_id
        self.session = ActiveSymbolSession(symbol, candles=[Candle(**c) for c in bootstrap["candles"]])
        self.instrument = InstrumentSpec(**bootstrap["instrument"])
        self.book_state = OrderBookState(50)
        self.epoch = 0
        self.start_ns = mono_ns
        self.last_book_ns = 0
        self.last_event_ns = mono_ns
        self.observed_ms = 0
        self.sequence = 0
        self.health_reason = "snapshot_required"
        # Reuse existing kline/overlay implementations without constructing a bot.
        self.candle_runtime = SimpleNamespace(config=SimpleNamespace(bootstrap_1m_candles=720),
            _overlay_trade_on_forming_candle=TradingEngine._overlay_trade_on_forming_candle)

    def invalidate(self, mono_ns, reason):
        self.epoch += 1
        self.start_ns = mono_ns
        self.last_book_ns = 0
        self.book_state._clear()
        self.session.orderbook = OrderBook()
        self.session.trades.clear()
        self.session.book_flow.clear()
        self.health_reason = reason

    def apply(self, event):
        now = event["processingMonoNs"]
        if now < self.last_event_ns:
            raise ValueError("capture availability order regressed")
        self.last_event_ns, self.sequence = now, event["sequence"]
        b = event["body"]
        if event["kind"] == "transport":
            if b["phase"] in {"connecting", "fault", "cancelled"} and f"orderbook.50.{self.symbol}" in b["topics"]:
                self.invalidate(now, "transport_"+b["phase"])
            return False
        if event["kind"] != "market_message":
            return False
        topic = b.get("topic", "")
        self.observed_ms = max(self.observed_ms, int(b.get("cts") or b.get("ts") or 0))
        if topic.startswith("orderbook.50."):
            previous = self.session.orderbook
            if self.last_book_ns and now-self.last_book_ns > 1_500_000_000:
                # A silence does not prove missing deltas, but invalidates warmup/labels.
                self.start_ns = now
                self.epoch += 1
                self.session.trades.clear()
                self.session.book_flow.clear()
            try:
                self.session.orderbook = self.book_state.apply(b)
            except OrderBookSequenceError:
                self.invalidate(now, "sequence_gap")
                return False
            if self.session.orderbook.mid is None or self.session.orderbook.best_bid >= self.session.orderbook.best_ask:
                self.invalidate(now, "invalid_book")
                return False
            if previous.mid is not None and b.get("type") != "snapshot":
                self.session.record_book_flow(self.observed_ms, best_level_ofi_usd(previous,self.session.orderbook))
            self.last_book_ns = now
            self.health_reason = None
            return True
        if topic.startswith("publicTrade."):
            for row in b.get("data", []):
                tick = TradeTick(int(row["T"]),float(row["p"]),float(row["v"]),row["S"])
                self.observed_ms = max(self.observed_ms,tick.ts_ms)
                self.session.trades.append(tick)
                TradingEngine._overlay_trade_on_forming_candle(self.session,tick)
                self.session.last_price=tick.price
            prune_trades(self.session.trades,self.observed_ms)
        elif topic.startswith("kline.1."):
            TradingEngine._apply_kline(self.candle_runtime,self.session,b)
        return False

    def snapshot(self, mono_ns):
        session, now_ms = self.session, self.observed_ms
        closed = [c for c in session.candles if c.confirmed and c.start_ms+60000<=now_ms]
        forming = max((c for c in session.candles if not c.confirmed and c.start_ms<=now_ms),
                      key=lambda c:c.start_ms,default=None)
        age = (mono_ns-self.last_book_ns)/1e9 if self.last_book_ns else None
        candle_age = (now_ms-(closed[-1].start_ms+60000))/1000 if closed else None
        execution = build_execution_context(book=session.orderbook,
            book_fresh=age is not None and 0<=age<=1.5, book_synced=self.book_state.synced,
            book_age_seconds=age, candle_fresh=candle_age is not None and 0<=candle_age<=150,
            candle_age_seconds=candle_age,trade_buffer_seconds=(mono_ns-self.start_ns)/1e9)
        flow = build_multi_horizon_flow_context(compute_trade_flow(list(session.trades),now_ms),
            session.book_flow_snapshot(now_ms),observed_at_ms=now_ms)
        forming_context=build_forming_candle_context(forming,closed,observed_at_ms=now_ms,
            recent_trades=list(session.trades),tape_updates=session.forming_trade_overlay_updates,
            last_trade_ts_ms=session.forming_trade_last_ts_ms or None,
            kline_snapshot_observed_at_ms=session.forming_kline_observed_at_ms or None,
            price_source=session.forming_price_source,volume_source=session.forming_volume_source)
        context=MarketContext(self.symbol,now_ms,session.orderbook.mid or session.last_price,Trend.FLAT,
            None,None,flow,None,None,execution,forming_context)
        windows=tuple(n for n in (5,15,60) if mono_ns-self.start_ns>=n*1_000_000_000)
        coverage=ContextCoverage(windows,windows,forming_context is not None,False,False,True)
        ref=SnapshotRef(self.capture_id,self.symbol,self.epoch,self.sequence,now_ms,mono_ns,
                        "capture:"+self.capture_id,FEATURE_SCHEMA)
        return extract_context_features(context,ref,coverage), context, coverage
