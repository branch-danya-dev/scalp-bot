"""Optional single-capture research observer. No decision/risk mutation.

Bounded V3 virtual paths receive the same causal ticks/context evaluations as the
ordinary broker. These labels require primary-capture and replay validation before
training. Research failure is visible and disables collection.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
import functools

from ..cross_venue import CrossVenueRuntime, VenueEvent, alignment
from ..cross_venue_public import PublicCrossVenueService
from ..maker_shadow import MakerShadowEngine
from ..segment_registry import SegmentRegistry
from ..setup_segments import setup_segment, closed_observation
from ..research_journal import ResearchJournal
from .prepared_labels import PreparedLabelEngine


def source_sequence(engine, now):
    return engine.input_journal.sequence if engine.input_journal else getattr(engine.recorder, "sequence", now)


def guarded(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        if self.failure:
            return
        try:
            if self.journal.writer.error:
                raise RuntimeError(self.journal.writer.error)
            return method(self, *args, **kwargs)
        except Exception as exc:
            self.failure = dict(stage=method.__name__, errorType=type(exc).__name__, reason=str(exc))
            self.journal.append("research_failure", self.failure)
    return call


class Wave2Observer:
    def __init__(self, path, capture_id, config, manifest):
        self.config = config
        self.capture_id = capture_id
        self.journal = ResearchJournal(path, capture_id, manifest)
        self.cross = CrossVenueRuntime(capture_id)
        self.service = PublicCrossVenueService(self.cross, self.journal)
        self.maker = MakerShadowEngine(config, self.journal.append)
        self.labels = PreparedLabelEngine(config, self._label, audit=lambda row:self.journal.append("label_input", row))
        self.registry = None
        self.epochs = defaultdict(int)
        self.last_post = {}
        self.failure = None

    def _label(self, kind, payload):
        self.journal.append(kind, payload)
        if kind == "prepared_label" and self.registry is not None:
            self.registry.book.record_shadow(payload)

    async def start(self):
        await self.service.start()

    def watch(self, symbols):
        if not self.failure:
            self.service.watch(symbols)

    def _registry(self, engine):
        if self.registry is None:
            self.registry = SegmentRegistry(engine.segment_expectancy)
        return self.registry

    @guarded
    def prepared(self, engine, session, decision, row):
        if row["source"]["capture_id"] != self.capture_id:
            raise ValueError("prepared source belongs to another capture")
        now = row["source"]["available_mono_ns"]
        snapshot = self.cross.snapshot(session.symbol, now)
        # Risk build receives a detached decision and unreserved current budgets.
        # It is an observation; it never grants ordinary admission authority.
        if decision is None:
            raise ValueError("missing strategy-owned prepared decision")
        staged = decision.details.get("stagedEntry", {}).get("phase") == "add"
        result = None if staged else engine._build_risk_plan_for_opportunity(session, deepcopy(decision),
            decision.setup_id, "open", None)
        plan = asdict(result.plan) if result is not None and result.plan is not None else None
        details = result.plan.strategy_details if plan else decision.details
        segment = setup_segment(decision.strategy, decision.action.value, details,
            entry=row["intent"]["entry"], stop=row["intent"]["stop"], context=session.market_context)
        registry = self._registry(engine).assess(segment, now_ms=row["available_wall_ms"])
        prepared = dict(row=row, frozenPlan=plan,
            economicsAllowed=bool(result and result.allowed and plan),
            economicReason=result.reason if result else "staged_add_requires_portfolio_path",
            segment=segment, segmentRegistry=registry, crossVenue=snapshot,
            crossVenueAlignment=alignment(snapshot, row["intent"]["side"]),
            funding=session.funding_public(), sourceSequence=row["source"]["source_sequence"],
            bookFresh=session.book_is_fresh(), depthFresh=session.deep_book_is_fresh(),
            planMeaning="frozen_first_prepared_structural_plan_unreserved",
            instrument=asdict(session.instrument) if session.instrument is not None else None,
            sourceCaptureId=self.capture_id, epoch=self.epochs[session.symbol])
        self.journal.append("prepared", prepared)
        self.labels.add(prepared)

    @guarded
    def book(self, engine, session, message, *, fast):
        now = engine.clock.perf_counter_ns()
        epoch = self.epochs[session.symbol]
        fresh = session.deep_book_is_fresh() and session.book_is_fresh()
        if fast and session.orderbook.mid and message.receipt_mono_ns:
            book = session.orderbook
            event = VenueEvent(self.capture_id, "bybit", session.symbol, epoch, "quote",
                int(message.cts or message.ts or 0), message.receipt_wall_ns//1_000_000,
                message.receipt_mono_ns, now, source_sequence(engine, now),
                book.best_bid, book.best_ask, book.bids[0][1], book.asks[0][1], units_verified=session.instrument is not None)
            accepted = self.cross.ingest(event)
            self.journal.append("venue_event", dict(event=asdict(event), accepted=accepted))
        # Deep-only updates can resolve executable markouts but never create a quote.
        book = session.depth_orderbook()
        self.maker.book(session.symbol, epoch=epoch, now_ns=now, book=book, depth_fresh=fresh)
        self.labels.market(session.symbol, now_ns=now, wall_ms=int(engine.clock.time()*1000),
            sequence=source_sequence(engine, now), epoch=epoch,
            book=session.orderbook, depth=book, fresh=fresh,
            exchange_ms=session.latest_processed_event_ms, funding=session.funding_public())
        if not fast or not fresh or now-self.last_post.get(session.symbol, 0) < 1_000_000_000:
            return
        self.last_post[session.symbol] = now
        instrument = session.instrument
        if instrument is None:
            return
        external = self.cross.snapshot(session.symbol, now)
        context = session.market_context
        local = context.local_regime if context else None
        flow = context.flow if context else None
        bybit = external["venues"]["bybit"]
        bid_qty, ask_qty = session.orderbook.bids[0][1], session.orderbook.asks[0][1]
        for side in ("long", "short"):
            normalized = instrument.normalize_quantity(entry_price=session.orderbook.mid,
                requested_notional=100., market_order=False)
            if normalized:
                self.maker.post(session.symbol, side, epoch=epoch, now_ns=now,
                    sequence=source_sequence(engine, now),
                    exchange_ms=session.latest_processed_event_ms, book=session.orderbook,
                    quantity=normalized[0], depth_fresh=fresh,
                    context=dict(asOfMonoNs=now, sourceSequence=source_sequence(engine, now),
                        contextObservedMs=context.observed_at_ms if context else None,
                        crossVenueAlignment=alignment(external, side), crossVenue=external,
                        ofiUsd=bybit.get("ofiUsd"),tradeImpulse=bybit.get("tradeImpulse"),
                        imbalance=(bid_qty-ask_qty)/(bid_qty+ask_qty) if bid_qty+ask_qty else None,
                        regime=local.regime.value if local else None,
                        volatilityPct=local.recent_range_pct if local else None,
                        volatilityMeaning="last_causal_local_recent_range_pct",
                        depthFresh=fresh,flow=flow.public() if flow else None))

    @guarded
    def trade(self, engine, session, message, tick):
        now = engine.clock.perf_counter_ns()
        epoch = self.epochs[session.symbol]
        sequence = source_sequence(engine, now)
        self.maker.trade(session.symbol, epoch=epoch, now_ns=now, sequence=sequence,
            tick_sequence=tick.sequence, exchange_ms=tick.ts_ms, price=tick.price, quantity=tick.size,
            aggressor=tick.side, depth_fresh=session.deep_book_is_fresh() and session.book_is_fresh())
        self.labels.market(session.symbol, now_ns=now, wall_ms=int(engine.clock.time()*1000),
            sequence=sequence, epoch=epoch, book=session.orderbook, depth=session.depth_orderbook(),
            fresh=session.deep_book_is_fresh() and session.book_is_fresh(),
            exchange_ms=tick.ts_ms, tick=tick, funding=session.funding_public())
        if message.receipt_mono_ns and tick.side.lower() in {"buy", "sell"}:
            event = VenueEvent(self.capture_id, "bybit", session.symbol, epoch, "trade", tick.ts_ms,
                message.receipt_wall_ns//1_000_000, message.receipt_mono_ns, now, tick.sequence,
                price=tick.price, quantity=tick.size, side=tick.side.lower(), units_verified=session.instrument is not None)
            accepted = self.cross.ingest(event)
            self.journal.append("venue_event", dict(event=asdict(event), accepted=accepted))

    @guarded
    def gap(self, engine, symbol, reason):
        self.epochs[symbol] += 1
        self.cross.gap("bybit", symbol, self.epochs[symbol], reason)
        now = engine.clock.perf_counter_ns()
        self.maker.invalidate(symbol, reason, now)
        self.labels.invalidate(symbol, reason, now, int(engine.clock.time()*1000))
        self.journal.append("venue_gap", dict(venue="bybit", symbol=symbol,
            epoch=self.epochs[symbol], reason=reason, nowNs=now))

    @guarded
    def closed_position(self, engine, session, event):
        observation = closed_observation(event, session.symbol)
        # PR60 already recorded the position. Attach provenance to its lifecycle
        # evidence separately in future persisted imports; never record it twice.
        result = self._registry(engine).assess(observation["segment"],
            now_ms=int(engine.clock.time()*1000), population="closed_positions")
        self.journal.append("segment_position", dict(observation=observation, registry=result))

    async def close(self, now_ns):
        await self.service.close()
        self.maker.close(now_ns)
        for path in list(self.labels.pending.values()):
            self.labels.invalidate(path.plan.symbol, "capture_end", now_ns,
                path.start_wall_ms+int((now_ns-path.start_ns)/1e6))
        self.journal.close()

    @guarded
    def context(self, engine, session):
        if not self.labels.pending:
            return
        clock = engine._clock_state(session)
        wall_ms = int(engine.clock.time()*1000)
        self.labels.context(session, now_ns=engine.clock.perf_counter_ns(), wall_ms=wall_ms,
            clock_valid=clock is None or clock["valid"],
            observed_ms=clock["evaluationMs"] if clock is not None else wall_ms)

    def health(self):
        return dict(failure=self.failure, writer=self.journal.health(), mode="shadow_only")
