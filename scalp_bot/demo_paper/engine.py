"""One production scanner/market pipeline with explicit paired execution hooks."""
from collections import deque
import time
from .contracts import Intent, SafetyError
from ..engine import TradingEngine
from ..ml.contracts import SnapshotRef
from ..ml.features import FEATURE_SCHEMA, ContextCoverage, extract_context_features

class PairedEngine(TradingEngine):
    def __init__(self,config,portfolio,worker,adapter,**kwargs):
        super().__init__(config,**kwargs)
        self.portfolio=portfolio;self.broker=portfolio;self.worker=worker;self.adapter=adapter
        self.source={};self.epochs={};self.covered_since={};self.grid={};self.ml_pending=deque(maxlen=32)
        self.ml_current={};self.ml_quotes={};self.sequence=0
        if self.input_journal is not None:
            original_market_record=self.input_journal.market_message
            def recorded(symbol,message):
                original_market_record(symbol,message)
                receipt=message.get("receipt_mono_ns")
                if receipt is not None:
                    self.source[symbol]=(self.input_journal.sequence,int(receipt))
                    self.covered_since.setdefault(symbol,int(receipt))
            self.input_journal.market_message=recorded
        for arm in portfolio.arms.values():
            arm.broker.position_manager=lambda pos,gross:(self.strategies[pos.strategy].manage_progress(
                self.config,self.clock,pos,gross) if pos.strategy in self.strategies else False)

    def _market_stream_options(self):
        from .transport import NoRedirectConnect
        from .preflight import PUBLIC_WS
        def connect(url,**kwargs):
            if url!=PUBLIC_WS:raise SafetyError("public WS host forbidden")
            return NoRedirectConnect(url,proxy=None,**kwargs)
        return {"connect_factory":connect}

    def _market_handler(self,symbol):
        handler,fast,deep=super()._market_handler(symbol)
        async def apply(message):
            receipt=message.get("receipt_mono_ns")
            if receipt is not None and self.input_journal is None:
                self.sequence+=1
                self.source[symbol]=(getattr(self.recorder,"sequence",0) or self.sequence,int(receipt))
                self.covered_since.setdefault(symbol,int(receipt))
            await handler(message)
        return apply,fast,deep

    async def _fetch_bootstrap_result(self,symbol):
        result=await super()._fetch_bootstrap_result(symbol)
        venue=self.portfolio.arms["demo"].venue
        rows=await venue.rest.pages("/v5/position/list",dict(category="linear",symbol=symbol))
        from .contracts import dec
        if not rows or any(int(r["positionIdx"])!=0 or dec(r["size"])!=0 for r in rows):
            raise SafetyError("symbol position mode/exposure not verified before paired admission")
        self.portfolio.emit("symbol_preflight",dict(symbol=symbol,position_mode=0,
            actual_leverage=[r.get("leverage") for r in rows],instrument=result[0].public()))
        return result

    def _can_deactivate(self,session,now):
        if session.symbol in self.portfolio.by_symbol:return False
        return super()._can_deactivate(session,now)

    def _submit_research_opportunity(self,best):
        source=self.source.get(best.session.symbol)
        if source is None:return True
        if time.perf_counter_ns()-source[1]>int(self.config.market_stale_seconds*1e9):return True
        intent=Intent.freeze(self.portfolio.run,best.plan,*source)
        allowed,reason=self.portfolio.admit(intent,best.session.instrument)
        self.recorder.record("paired_admission",best.session.symbol,dict(pair_id=intent.pair_id,allowed=allowed,reason=reason))
        if allowed:
            self._mark_admission_fire(best)
            self.router.submitted(best.session.symbol,self.clock.perf_counter_ns()/1e9,strategy=best.decision.strategy)
            self._scenario_events()
        return True

    def _route_scenario(self,session,candles):
        pos=self.broker.positions.get(session.symbol)
        if pos is None or pos.strategy!="trend_impulse_ml":return super()._route_scenario(session,candles)
        previous=self.router.all_for(session.symbol)
        result=self.router.observe(session.symbol,session.market_context,candles,session.structure,
            self.strategy_enabled,self.clock.perf_counter_ns()/1e9)
        for key,current in self.router.all_for(session.symbol).items():
            if current is not previous.get(key):self.strategies[key].reset(session.symbol)
        session.scenario_view=self.router.public(session.symbol);self._scenario_events()
        return result

    def _mark_execution_from_market(self,session,**kwargs):
        self._market_for_pair(session,**kwargs)

    def _mark_position_from_book(self,session,**kwargs):
        self._market_for_pair(session,**kwargs)

    def _paired_execution_book(self, session):
        from ..execution_book import coherent_execution_book
        return coherent_execution_book(session.orderbook,session.depth_orderbook() if session.deep_book_is_fresh() else None)

    def _market_for_pair(self,session,*,trade_price=None,trade_notional_usd=None,trade_side=None,**kwargs):
        if not session.book_is_fresh():
            self.portfolio.books.pop(session.symbol,None)
            self.portfolio.arms["paper"].venue.books.pop(session.symbol,None)
            if session.symbol in self.portfolio.by_symbol:self.portfolio.halt("market_book_unhealthy")
            return
        book=self._paired_execution_book(session)
        self.portfolio.books[session.symbol]=book
        receipt=session.trade_receipt_mono
        self.portfolio.arms["paper"].venue.market(session.symbol,book,trade_price=trade_price,
            trade_notional=trade_notional_usd,trade_side=trade_side,
            receipt_ns=None if receipt is None else int(receipt*1e9))
        clock=self._clock_state(session)
        observed_ms=clock["evaluationMs"] if clock and clock["valid"] else (int(self.clock.time()*1000) if clock is None else 0)
        for name,arm in self.portfolio.arms.items():
            pos=arm.broker.positions.get(session.symbol)
            due=session.next_funding_time_ms
            if pos is None or due is None or observed_ms is None:continue
            if name=="paper":
                event=arm.broker._apply_funding_if_due(pos,funding_rate=session.funding_rate,funding_time_ms=due,
                    mark_price=session.mark_price,observed_at_ms=observed_ms)
                if event:self.portfolio.emit("funding",dict(arm=name,pair_id=self.portfolio.by_symbol[session.symbol],row=event))
            elif pos.opened_exchange_ms is not None and pos.opened_exchange_ms<due<=observed_ms:
                self.portfolio.emit("funding_due",dict(arm=name,pair_id=self.portfolio.by_symbol[session.symbol],symbol=session.symbol,due_ms=due))
        self.portfolio.dirty.set()

    def _validate_pending_entry(self,session):
        pair=self.portfolio.by_symbol.get(session.symbol)
        if not pair:return
        for arm in self.portfolio.arms.values():
            for order in arm.active_orders(pair):
                if order.command.reduce_only:continue
                plan=arm.intents[pair].plan()
                if self._clock_entry_block(session):self.portfolio.halt("pending_clock_invalid")
                if plan.strategy in self.strategies:
                    decision=session.decisions.get(plan.strategy)
                    if decision and self.strategies[plan.strategy].entry_invalidation(decision,session.orderbook):
                        self.portfolio.cancel_entries.add(pair)

    def _maybe_strategy_invalidation(self,session):
        pair=self.portfolio.by_symbol.get(session.symbol)
        if not pair:return
        clock=self._clock_state(session)
        if clock is not None and not clock["valid"]:return
        for arm in self.portfolio.arms.values():
            pos=arm.broker.positions.get(session.symbol)
            if pos is None or pos.strategy not in self.strategies:continue
            if not pos.strategy_details.get("scenario") and self.clock.perf_counter_ns()/1e9-pos.opened_mono<self.config.strategy_invalidation_grace_seconds:continue
            reason=self.strategies[pos.strategy].manage_position(side=pos.side,unrealized_pnl=pos.unrealized_pnl,
                opened_at=pos.opened_at,strategy_details=pos.strategy_details,decision=session.decisions.get(pos.strategy),
                trend=session.trend,last_price=session.last_price,book=session.orderbook,market_context=session.market_context,
                observed_at_ms=clock["evaluationMs"] if clock else int(self.clock.time()*1000))
            if reason:self.portfolio.exit_reasons[(arm.name,pair)]=reason

    def _invalidate_transport(self,symbol,event,fast_state,deep_state):
        # Original invalidation/recovery remains authoritative; no stale book reuse.
        if event.get("phase") not in {"connecting","fault","cancelled"}:return
        super()._invalidate_transport(symbol,event,fast_state,deep_state)
        self.portfolio.books.pop(symbol,None)
        self.portfolio.arms["paper"].venue.books.pop(symbol,None)
        self.epochs[symbol]=self.epochs.get(symbol,0)+1;self.covered_since.pop(symbol,None)
        if self.worker:self.worker.deactivate(symbol)
        if symbol in self.portfolio.by_symbol:self.portfolio.halt("public_transport_gap")

    def _cancel_all_pending(self,reason):
        if reason=="clock_invalid" and not self.portfolio.has_execution_work:
            # The ordinary arbiter cancels pending entries while time is not
            # synchronized. An empty startup has nothing to cancel or close;
            # retain the clock retry loop instead of terminating the hour.
            self.portfolio.accepting=False
            return
        self.portfolio.halt(reason)
    def _close_all_positions(self,reason):self.portfolio.halt(reason)
    def _stop_trading(self,reason,**kwargs):
        self.running=False;self.portfolio.halt(reason)
    def _launch_run_timer(self):pass  # The coordinator owns the sole monotonic deadline.

    async def _evaluate(self,session):
        await super()._evaluate(session)
        if not self.worker or not self.worker.ready or not self.portfolio.accepting:return
        context=session.market_context;source=self.source.get(session.symbol)
        if context is None or source is None or session.instrument is None:return
        bucket=context.observed_at_ms//10000
        if self.grid.get(session.symbol)==bucket:return
        if source[1]-self.covered_since.get(session.symbol,source[1])<60_000_000_000:return
        if not session.book_is_fresh() or self._clock_entry_block(session):return
        self.grid[session.symbol]=bucket
        ref=SnapshotRef(self.portfolio.run,session.symbol,self.epochs.get(session.symbol,0),source[0],
            context.observed_at_ms,source[1],"perf_counter",FEATURE_SCHEMA)
        start=time.perf_counter_ns()
        coverage=ContextCoverage((5,15,60),(5,15,60),context.forming_candle is not None,
            session.deep_book_is_fresh(),context.structure is not None,True)
        snapshot=extract_context_features(context,ref,coverage)
        self.ml_current[session.symbol]=ref
        self.worker.activate(session.symbol,ref.selection_epoch)
        from ..domain import Side
        for side in ("long","short"):
            if len(self.ml_pending)==self.ml_pending.maxlen:
                self.recorder.record("ml_queue_reject",session.symbol,dict(source_sequence=ref.source_sequence,side=side));continue
            self.ml_pending.append((snapshot,side))
            self.ml_quotes[(ref.symbol,ref.source_sequence,side)]=session.orderbook.executable_entry(Side(side))
        self.portfolio.emit("ml_features",dict(symbol=session.symbol,ref=__import__("dataclasses").asdict(ref),
            features=snapshot.values,feature_start_ns=start,feature_end_ns=time.perf_counter_ns()))

    def poll_ml(self):
        if not self.worker:return
        for item in self.worker.poll():
            if item[0]!="forecast":
                self.recorder.record("ml_worker_error",None,dict(reason=item[1]));continue
            forecast=item[1];received=time.perf_counter_ns()
            self.portfolio.emit("ml_predict",dict(symbol=forecast.source.symbol,source_sequence=forecast.source.source_sequence,
                side=forecast.side,predict_end_ns=forecast.produced_mono_ns,predict_ns=item[2],received_ns=received))
            session=self.sessions.get(forecast.source.symbol)
            source_quote=self.ml_quotes.pop((forecast.source.symbol,forecast.source.source_sequence,forecast.side),None)
            if session is None:
                from dataclasses import asdict
                self.portfolio.emit("ml_decision",dict(forecast=asdict(forecast),reasons=["symbol_inactive"],received_ns=received))
                continue
            current=self.ml_current.get(session.symbol);blocked=[]
            if current is None:blocked.append("source_context_missing")
            if self._clock_entry_block(session):blocked.append("market_clock_invalid")
            rule_ready=False
            if forecast.p_target_first>=.55 and not blocked:
                for decision in session.decisions.values():
                    if decision.tradeable and self._build_risk_plan_for_opportunity(session,decision,
                            decision.setup_id or self._resolve_setup_id(session,decision),"open",None).allowed:
                        rule_ready=True;break
            self.adapter.accept(forecast,current or forecast.source,session,received,source_quote,
                                rule_ready=rule_ready,blocked_reasons=blocked)
        if self.worker.failed:
            self.ml_pending.clear()  # Protection and ordinary strategies continue in parent.
            return
        if self.worker.ready and not self.worker.inflight and not self.worker.latest and self.ml_pending:
            snapshot,side=self.ml_pending.popleft()
            if time.perf_counter_ns()-snapshot.ref.available_mono_ns<1_000_000_000:
                submitted=self.worker.submit(snapshot,side)
                self.portfolio.emit("ml_submit",dict(symbol=snapshot.ref.symbol,source_sequence=snapshot.ref.source_sequence,
                    side=side,submitted=submitted,worker_dispatch_ns=self.worker.inflight[1] if self.worker.inflight else None))
            else:self.recorder.record("ml_queue_expired",snapshot.ref.symbol,dict(source_sequence=snapshot.ref.source_sequence,side=side))
