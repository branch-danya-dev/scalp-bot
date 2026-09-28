"""Explicit owner-started one-hour runtime; never invoked by imports or tests."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import time
import uuid
from .contracts import SafetyError, dec
from .engine import PairedEngine
from .ml_adapter import ResearchMLAdapter
from .portfolio import Arm, PairedPortfolio
from .preflight import PublicRest, connected_preflight
from .transport import DemoRest, private_stream
from .venues import DemoVenue, PaperVenue
from ..ml.worker import InferenceWorker
from ..recorder import SessionRecorder
from ..runtime_clock import SystemRuntimeClock


def start_worker_without_secrets(worker):
    # Windows spawn inherits environment, so scrub private credential variables
    # across process creation; restore the parent's environment immediately.
    names=[k for k in os.environ if any(t in k.upper() for t in ("API_KEY","API_SECRET","DEMO_EXPECTED_UID"))]
    saved={k:os.environ.pop(k) for k in names}
    try:worker.start()
    finally:os.environ.update(saved)


def install_stop_signals(stop):
    previous={}
    def handler(signum,frame):stop.set()
    for name in ("SIGINT","SIGTERM","SIGBREAK"):
        value=getattr(signal,name,None)
        if value is not None:
            previous[value]=signal.getsignal(value);signal.signal(value,handler)
    return lambda:[signal.signal(key,value) for key,value in previous.items()]


class Session:
    def __init__(self,config,metadata,model_dir,credentials,preflight,output,*,rule_only=False,engine_class=PairedEngine):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=False)
        self.config=config;self.preflight=preflight;self.run=uuid.uuid4().hex
        self.clock=SystemRuntimeClock();self.recorder=SessionRecorder(str(self.output/"capture"),clock=self.clock,
            queue_size=config.recorder_queue_size,critical_enqueue_timeout_seconds=config.recorder_critical_enqueue_timeout_seconds)
        self.recorder.start_background_writer()
        self.events=[];self.stop=asyncio.Event();self.private_ready=asyncio.Event();self.finished=False
        self.credentials=credentials;self.rest=DemoRest(credentials,enabled=True);self.public=PublicRest(config)
        self.worker=None if rule_only else InferenceWorker(model_dir);self.tasks=[];self.start_ns=None;self.start_ms=None;self.funding_seen=set();self.loop_lateness_ms=[];self.adapter_ms=[];self.opened_pairs=set();self.expected_funding=set();self.observed_funding=set()
        self.arms={n:Arm(n,config,self.clock,self.emit) for n in ("demo","paper")}
        self.arms["demo"].venue=DemoVenue(self.rest,lambda e,p:self.emit(e,dict(arm="demo",**p)),self.arms["demo"].fill)
        self.arms["paper"].venue=PaperVenue(config,lambda e,p:self.emit(e,dict(arm="paper",**p)),self.arms["paper"].fill)
        self.arms["paper"].venue.remaining=self.arms["paper"].remaining
        self.portfolio=PairedPortfolio(self.run,config,self.arms,self.emit)
        self.adapter=None if rule_only else ResearchMLAdapter(metadata,self.portfolio,self.emit)
        self.engine=engine_class(config,self.portfolio,self.worker,self.adapter,rest_client=self.public,
            recorder=self.recorder,capture_inputs=not rule_only,configure_observability=False)
        self.rest.before_write=self.before_write
        self._entry_readiness_key=None;self.account_task=None

    def before_write(self,path,params):
        if path!="/v5/order/create":return
        order=self.arms["demo"].venue.orders.get(params.get("orderLinkId"))
        if order is None:raise SafetyError("unregistered private order")
        if not order.command.reduce_only:
            intent=self.arms["demo"].intents[order.command.pair_id]
            session=self.engine.sessions.get(order.command.symbol)
            if not self.portfolio.accepting or not self.private_ready.is_set():raise SafetyError("admission stopped before send")
            if session is None or not session.book_is_fresh() or self.engine._clock_entry_block(session):
                raise SafetyError("entry source invalid before send")
            max_age=1_000_000_000 if intent.author=="ml" else int(self.config.market_stale_seconds*1e9)
            if time.perf_counter_ns()-intent.observed_ns>=max_age:raise SafetyError("entry expired in execution queue")
            source=intent.plan().setup_entry;quote=session.orderbook.executable_entry(intent.plan().side)
            if quote is None or abs(quote/source-1)*10000>self.config.max_entry_drift_bps:
                raise SafetyError("entry drift before send")
        # DemoRest invokes this after its serialized queue/pacing wait.
        # This is application HTTP-send start, not exchange execution time.
        order.sent_ns=time.perf_counter_ns()
        self.emit("send",dict(link_id=order.command.link_id,pair_id=order.command.pair_id,ns=order.sent_ns))

    def emit(self,event,payload):
        if event=="funding_due":
            key=(payload["pair_id"],payload["symbol"],payload["due_ms"])
            if key in self.expected_funding:return
            self.expected_funding.add(key)
        row=dict(event=event,payload=payload,received_ns=time.perf_counter_ns(),wall_ms=int(time.time()*1000))
        self.events.append(row);self.recorder.record("demo_paper_"+event,payload.get("symbol"),payload)
        if event=="venue_failure" and hasattr(self,"portfolio"):self.portfolio.halt(payload["reason"])
        if event=="position_entry_fill" and payload["pair_id"] not in self.opened_pairs:
            pair=payload["pair_id"];self.opened_pairs.add(pair)
            intent=self.arms[payload["arm"]].intents[pair];plan=intent.plan()
            if intent.author=="rule":
                from ..domain import Action,StrategyDecision
                decision=StrategyDecision(plan.strategy,Action(plan.side.value),[],entry=plan.setup_entry,
                    stop=plan.stop,target=plan.target,setup_id=plan.setup_id,details=plan.strategy_details)
                self.engine.strategies[plan.strategy].mark_opened(plan.symbol,decision)
                self.engine.router.restore_execution(plan.symbol,plan,self.clock.perf_counter_ns()/1e9)
                self.engine.router.filled(plan.symbol,self.clock.perf_counter_ns()/1e9)
        if event=="pair_reconciled":
            pair=payload["pair_id"];intent=self.arms["paper"].intents[pair]
            if intent.author=="rule":
                plan=intent.plan();self.engine.router.completed(plan.symbol,self.clock.perf_counter_ns()/1e9,"both_arms_reconciled")
                market=self.engine.sessions.get(plan.symbol)
                if market is not None and pair in self.opened_pairs:self.engine._consume_setup(market,plan.strategy,plan.setup_id)
                # Retain ordinary expectancy telemetry on its paper ledger; the
                # frozen profile has the expectancy gate disabled.
                trades=[t for t in self.arms["paper"].broker.closed_trades if t["setupId"]==plan.setup_id]
                for trade in trades:self.engine.expectancy.record(plan.strategy,net_pnl_usd=trade["netPnl"],initial_risk_usd=trade["initialRiskUsd"])

    async def reconcile(self,*,final=False):
        venue=self.arms["demo"].venue;positions=await venue.reconcile(force=final)
        for symbol in set(positions)|set(self.arms["demo"].broker.positions):
            row=positions.get(symbol);actual=dec(row["size"]) if row else dec(0)
            expected=self.arms["demo"].remaining(symbol)
            if actual!=expected:
                raise SafetyError("position/execution reconciliation mismatch")
            if actual and row["side"]!=("Buy" if self.arms["demo"].broker.positions[symbol].side.value=="long" else "Sell"):
                raise SafetyError("position direction mismatch")
        self.portfolio.release_reconciled()
        self.emit("account_reconciled",dict(positions={s:{k:r.get(k) for k in ("size","side","positionIdx")} for s,r in positions.items()},
            research_balances={n:a.broker.balance for n,a in self.arms.items()}))

    async def gap(self,reason):
        if reason=="reconnected":
            await self.reconcile();self.private_ready.set()
        else:
            self.private_ready.clear()
            if self.portfolio.accepting:self.portfolio.halt(reason)
            self.emit("private_gap",dict(reason=reason))

    async def private_message(self,message):
        try:await self.arms["demo"].venue.message(message)
        except Exception:
            self.portfolio.halt("private_fact_invalid");raise

    async def manage(self,name):
        while not self.finished:
            try:await self.portfolio.manage_arm(name)
            except Exception as exc:
                self.arms[name].venue.fail(type(exc).__name__);self.portfolio.halt("management_failure")
            await asyncio.sleep(.02)

    async def account_loop(self):
        while not self.finished:
            try:
                await self.reconcile()
                await self.funding()
            except Exception as exc:
                self.portfolio.halt("reconciliation_failure");self.emit("reconcile_failure",dict(error_type=type(exc).__name__))
            await asyncio.sleep(2)

    async def funding(self):
        if self.start_ms is None:return
        rows=await self.rest.pages("/v5/account/transaction-log",dict(accountType="UNIFIED",category="linear",
            currency="USDT",startTime=self.start_ms,endTime=int(time.time()*1000),limit=50))
        for row in rows:
            if row.get("type")!="SETTLEMENT":continue
            key=str(row["id"])
            if key in self.funding_seen:continue
            if row.get("currency")!="USDT":raise SafetyError("unknown funding currency")
            symbol=row["symbol"]
            # The dedicated account was empty at Start; every nonzero position
            # during this period must belong to one recorded experiment intent.
            matches=[i for i in self.arms["demo"].intents.values() if i.plan().symbol==symbol]
            if not matches:raise SafetyError("foreign settlement")
            stamp=int(row["transactionTime"])
            candidates=[]
            for intent in matches:
                entries=[b for b in self.arms["demo"].bookings if b["pair_id"]==intent.pair_id
                         and int(b["execTime"])<=stamp]
                signed=sum((dec(b["execQty"])*(1 if b["side"]==("Buy" if intent.plan().side.value=="long" else "Sell") else -1) for b in entries),dec(0))
                if signed>0:candidates.append(intent)
            if len(candidates)!=1:raise SafetyError("funding ownership unresolved")
            # transaction-log funding is signed cash flow; never use wallet equity deltas.
            amount=float(dec(row["funding"]))
            self.arms["demo"].broker.balance+=amount
            self.funding_seen.add(key)
            self.observed_funding.add((candidates[0].pair_id,symbol,stamp))
            self.emit("funding",dict(arm="demo",pair_id=candidates[0].pair_id,id=key,amount=amount,row=row))
    def funding_complete(self):
        return all(any(pair==p and symbol==s and abs(stamp-due)<=2000 for p,s,stamp in self.observed_funding)
                   for pair,symbol,due in self.expected_funding)

    def refresh_admission(self):
        """Wait without exposure; never bypass clock bounds or resurrect Stop."""
        clock=self.engine._clock_state()
        clock_valid=clock is None or clock["valid"]
        if not clock_valid and self.portfolio.has_execution_work:
            self.portfolio.halt("clock_invalid")
        reason=self.portfolio.stop_reason
        if reason is None and self.stop.is_set():reason="operator_stop"
        if reason is None and not self.private_ready.is_set():reason="private_not_ready"
        if reason is None and not clock_valid:reason="clock:"+str(clock["reason"])
        self.portfolio.accepting=reason is None
        key=(self.portfolio.accepting,reason)
        if key!=self._entry_readiness_key:
            self._entry_readiness_key=key
            self.emit("entry_readiness",dict(accepting=self.portfolio.accepting,reason=reason,clock=clock))

    async def run_session(self):
        restore=install_stop_signals(self.stop)
        reason="preflight_failed";complete=False
        try:
            connected=await connected_preflight(self.rest,self.public,self.credentials)
            self.emit("connected_preflight",connected)
            start_worker_without_secrets(self.worker)
            until=time.monotonic()+30
            while not self.worker.ready and not self.worker.failed and time.monotonic()<until:
                self.worker.poll();await asyncio.sleep(.01)
            if not self.worker.ready:raise SafetyError("model worker did not become ready")
            self.tasks.append(asyncio.create_task(private_stream(self.credentials,self.private_message,self.gap,self.stop,diagnostics=self.emit)))
            await asyncio.wait_for(self.private_ready.wait(),30)
            # One monotonic Start; bootstrap, pauses and worker failures consume it.
            self.start_ns=time.perf_counter_ns();self.start_ms=int(time.time()*1000)
            self.portfolio.started_ns=self.start_ns
            self.emit("start",dict(start_ns=self.start_ns,deadline_ns=self.start_ns+3_600_000_000_000))
            self.rest.write_enabled=True
            self.tasks.extend(asyncio.create_task(self.portfolio.dispatch(n)) for n in self.arms)
            self.tasks.extend(asyncio.create_task(self.manage(n)) for n in self.arms)
            self.account_task=asyncio.create_task(self.account_loop())
            self.tasks.append(self.account_task)
            await asyncio.wait_for(self.engine.start(),30)
            self.engine.running=True;self.refresh_admission()
            while not self.stop.is_set() and not self.portfolio.stop_reason and time.perf_counter_ns()-self.start_ns<3_600_000_000_000:
                self.refresh_admission()
                self.engine.poll_ml()
                health=self.recorder.health()
                if health["writerError"] or health["droppedRows"]:self.portfolio.halt("recorder_failure")
                for name,arm in self.arms.items():
                    mark=sum(p.unrealized_pnl for p in arm.broker.positions.values())
                    if arm.broker.total_pnl+mark<=-30:self.portfolio.halt("session_loss_bound")
                for symbol in self.portfolio.by_symbol:
                    session=self.engine.sessions.get(symbol)
                    if session is None or not session.book_is_fresh():self.portfolio.halt("market_freshness_failure")
                if time.perf_counter_ns()-self.arms["demo"].venue.last_reconcile_ns>30_000_000_000:
                    self.portfolio.halt("private_snapshot_stale")
                before=time.perf_counter_ns()
                await asyncio.sleep(.01)
                self.loop_lateness_ms.append(max(0,(time.perf_counter_ns()-before)/1e6-10))
            reason=self.portfolio.stop_reason or ("operator_stop" if self.stop.is_set() else "duration_elapsed")
        except Exception as exc:
            reason="failure_"+type(exc).__name__;self.emit("run_failure",dict(error_type=type(exc).__name__))
        finally:
            self.engine.running=False;self.portfolio.halt(reason)
            # Signal stops admission, not protection/reconciliation. The WS stop
            # event may be set, so REST remains the authoritative shutdown path.
            deadline=time.monotonic()+90
            # Do not queue two full account scans on the serialized REST client.
            # Execution/protection and private facts remain active.
            if self.account_task is not None:
                self.account_task.cancel()
                await asyncio.gather(self.account_task,return_exceptions=True)
            stable_flat=0
            while self.start_ns and time.monotonic()<deadline:
                try:
                    await asyncio.wait_for(self.reconcile(final=True),min(15,max(.01,deadline-time.monotonic())))
                    if not self.portfolio.reservations and not self.portfolio.dispatching:
                        stable_flat+=1
                        if stable_flat>=2:
                            await asyncio.wait_for(self.funding(),max(.01,min(10,deadline-time.monotonic())))
                            if self.funding_complete():complete=True;break
                    else:stable_flat=0
                except Exception as exc:
                    stable_flat=0;self.emit("shutdown_reconcile_failure",dict(error_type=type(exc).__name__))
                await asyncio.sleep(min(1,max(0,deadline-time.monotonic())))
            self.finished=True;self.stop.set()
            for task in self.tasks:task.cancel()
            await asyncio.gather(*self.tasks,return_exceptions=True)
            self.worker.close()
            try:await self.engine.close()
            finally:await self.rest.close();restore()
            from .report import write_report
            result=write_report(self,complete,reason)
            return result
