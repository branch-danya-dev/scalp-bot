"""Causal market application with bounded pending-order evaluation."""
from __future__ import annotations

from .domain import TradeTick
from .strategy.flow import prune_trades
from .strategy import compute_trade_flow, build_multi_horizon_flow_context
from .latency_observability import observe_latency


class MarketRuntime:
    async def _evaluate_pending_entry(self, session):
        started = self.clock.perf_counter_ns()
        clock = self._clock_state(session)
        now_ms = clock["evaluationMs"] if clock else int(self.clock.time()*1000)
        if session.market_context is not None:
            flow = compute_trade_flow(list(session.trades), now_ms)
            session.flow_context = build_multi_horizon_flow_context(flow,
                session.book_flow_snapshot(now_ms), observed_at_ms=now_ms)
            # Closed structural/regime state is unchanged by a trade tick.
            # Refresh only execution and flow; no density, routing or strategies.
            self._commit_market_context(session, observed_at_ms=now_ms, emit=False)
        self._validate_pending_entry(session)
        observe_latency("pending_causal_evaluator", (self.clock.perf_counter_ns()-started)/1e9)

    async def _process_public_trade_message(
        self,
        session: ActiveSymbolSession,
        message: MarketMessage,
    ) -> list[TradeTick]:
        """Apply one public-trade batch without leaking later ticks backward.

        A resting maker order must see each exchange trade in causal order:
        the current tick may fill it, then strategy invalidation may react to
        that tick. Later trades from the same websocket batch must never be
        visible to an earlier fill/cancel decision.
        """
        rows = message.get("data") or []
        if not rows:
            return []

        ticks: list[TradeTick] = []
        wall_now = self.clock.time()
        for row in rows:
            session.trade_sequence += 1
            tick = TradeTick(
                ts_ms=int(row.get("T") or self.clock.time() * 1000),
                price=float(row["p"]),
                size=float(row["v"]),
                side=str(row.get("S") or ""),
                sequence=session.trade_sequence,
            )
            session.trades.append(tick)
            self._overlay_trade_on_forming_candle(
                session,
                tick,
            )
            session.last_price = tick.price
            session.last_trade_stream_at = wall_now
            session.trade_receipt_mono = (message.get("receipt_mono_ns", 0) or 0) / 1e9 or None
            session.latest_processed_event_ms = max(session.latest_processed_event_ms, tick.ts_ms)
            session.trade_exchange_ts_ms = max(session.trade_exchange_ts_ms, tick.ts_ms)
            prune_trades(
                session.trades,
                tick.ts_ms,
                self.config.trade_buffer_seconds,
            )
            ticks.append(tick)

            # Execution caused by this exact trade has priority over strategy
            # invalidation caused by the same trade. Otherwise a fill can be
            # retroactively cancelled after the market already traded through.
            self._mark_execution_from_market(
                session,
                trade_ts_ms=tick.ts_ms,
                trade_price=tick.price,
                trade_notional_usd=tick.notional,
                trade_side=tick.side,
            )

            if self.research_observer is not None:
                self.research_observer.trade(self, session, message, tick)

            # If the resting order survived this tick, only now may the
            # strategy use this tick to decide whether the order is still
            # valid before the next exchange trade is processed.
            if session.symbol in self.broker.pending_entries:
                await self._evaluate_pending_entry(session)

        return ticks

