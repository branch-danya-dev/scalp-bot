"""Paired reservation and shared management; fills remain venue-owned facts."""
import asyncio
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal
import time
from .contracts import Command, Intent, SafetyError, dec
from ..domain import Side, OrderBook
from ..paper import PaperBroker
from ..execution import execution_profile

class Arm:
    def __init__(self,name,config,clock,emit):
        self.name=name;self.config=config;self.clock=clock;self.emit=emit
        self.broker=PaperBroker(config,clock=clock);self.venue=None
        self.intents={};self.orders_by_pair=defaultdict(list);self.revisions=defaultdict(int)
        self.partial_done=set();self.funding_ids=set();self.bookings=[];self.specs={}

    def fill(self,order,row,now_ns):
        command=order.command;plan=self.intents[command.pair_id].plan()
        qty,price,fee=map(lambda k:float(dec(row[k])),("execQty","execPrice","execFee"))
        pos=self.broker.positions.get(command.symbol)
        first=pos is None
        if not command.reduce_only:
            if pos is None:
                self.broker._position_from_fill(plan,price,fee,quantity=qty)
                self.broker.positions[command.symbol].opened_exchange_ms=int(row["execTime"])
            else:
                if pos.setup_id!=plan.setup_id or pos.side!=plan.side: raise SafetyError("position owner mismatch")
                old=pos.quantity
                pos.entry=(old*pos.entry+qty*price)/(old+qty)
                pos.quantity+=qty;pos.original_quantity+=qty
                pos.notional=pos.quantity*pos.entry;pos.original_notional+=qty*price
                pos.entry_fee_remaining+=fee;pos.entry_fee_total_usd+=fee
                pos.initial_risk_budget_usd+=qty*abs(price-plan.stop)
            pos=self.broker.positions[command.symbol]
            if plan.strategy=="trend_impulse_ml":
                spec=self.specs[command.pair_id];sign=1 if pos.side==Side.LONG else -1
                pos.initial_stop=pos.stop=spec.stop_price(pos.entry*(1-sign*.0015),pos.side)
                pos.target=spec.target_price(pos.entry*(1+sign*.003),pos.side)
            self.emit("position_entry_fill",dict(arm=self.name,pair_id=command.pair_id,first=first,
                symbol=command.symbol,position=self.broker.positions[command.symbol].public()))
        else:
            if pos is None or qty>pos.quantity+1e-9: raise SafetyError("close exceeds owned remainder")
            # Account a confirmed external price/fee once, without paper slippage.
            allocated=pos.entry_fee_remaining*qty/pos.quantity
            gross=(1 if pos.side==Side.LONG else -1)*(price-pos.entry)*qty
            leg=dict(fill=price,gross=gross,fees=fee+allocated,net=gross-fee-allocated,quantity=qty)
            preview=self.broker._preview_realize
            self.broker._preview_realize=lambda *args,**kwargs:leg
            try:
                if abs(qty-pos.quantity)<=1e-9:
                    event=self.broker.close(command.symbol,OrderBook(),command.reason)
                    self.emit("position_closed",dict(arm=self.name,pair_id=command.pair_id,trade=event))
                else:
                    self.broker._realize(pos,qty,None,reason=command.reason)
            finally: self.broker._preview_realize=preview
            if command.reason=="partial_take" and command.pair_id not in self.partial_done and order.filled==dec(command.qty):
                self.finish_partial(command.pair_id)
        self.bookings.append(dict(pair_id=command.pair_id,link_id=command.link_id,**row))

    def finish_partial(self,pair):
        intent=self.intents[pair];pos=self.broker.positions.get(intent.plan().symbol)
        if pos is not None:
            pos.partial_taken=True;pos.partial_taken_at=self.clock.time()
            self.broker._after_partial_fill(pos)
        self.partial_done.add(pair)

    def remaining(self,symbol):
        pos=self.broker.positions.get(symbol)
        return dec(pos.quantity) if pos else Decimal(0)

    def active_orders(self,pair):
        return [self.venue.orders[k] for k in self.orders_by_pair[pair] if k in self.venue.orders and not self.venue.orders[k].terminal]

    def command(self,run,intent,*,qty,mode,price=None,reduce=False,reason="entry"):
        plan=intent.plan();self.revisions[(intent.pair_id,reason)]+=1
        side="Buy" if plan.side==Side.LONG else "Sell"
        if reduce: side="Sell" if side=="Buy" else "Buy"
        c=Command.make(run,intent.pair_id,plan.symbol,side,qty,mode,price,reduce,reason,
                       self.revisions[(intent.pair_id,reason)],time.perf_counter_ns())
        self.orders_by_pair[intent.pair_id].append(c.link_id)
        return c

    def desired(self,intent,book,*,now_ns,stop_reason=None):
        """One function for both arms, evaluated against their actual positions.

        Bid/ask stops are local. Maker target/partial are resting limits, placed
        only after the entry fill; raw tape subsequently establishes paper fills.
        """
        plan=intent.plan();pos=self.broker.positions.get(plan.symbol)
        if pos is None: return []
        executable=book.executable_exit(pos.side) if book else None
        if executable is None:
            return [("health_stop",pos.quantity,"Market",None)] if stop_reason else []
        direction=1 if pos.side==Side.LONG else -1
        gross=direction*(executable-pos.entry)*pos.original_quantity
        pos.last_price=executable;pos.unrealized_pnl=direction*(executable-pos.entry)*pos.quantity-pos.entry_fee_remaining
        pos.mfe_usd=max(pos.mfe_usd,gross);pos.mae_usd=max(pos.mae_usd,-gross)
        pos.max_favorable_move_pct=max(pos.max_favorable_move_pct,direction*(executable-pos.entry)/pos.entry)
        pos.max_adverse_move_pct=max(pos.max_adverse_move_pct,-direction*(executable-pos.entry)/pos.entry)
        if stop_reason: return [(stop_reason,pos.quantity,"Market",None)]
        if (pos.side==Side.LONG and executable<=pos.stop) or (pos.side==Side.SHORT and executable>=pos.stop):
            return [("stop",pos.quantity,"Market",None)]
        if plan.strategy=="trend_impulse_ml":
            if now_ns-intent.observed_ns>=30_000_000_000: return [("timeout",pos.quantity,"Market",None)]
            if direction*(executable-pos.target)>=0: return [("target",pos.quantity,"Market",None)]
            return []
        if self.broker._should_cut_no_follow_through(pos,gross):
            return [("no_follow_through",pos.quantity,"Market",None)]
        profile=execution_profile(plan.strategy)
        partial=0.0;result=[];economics=pos.strategy_details.get("economics",{})
        if self.config.partial_take_enabled and pos.strategy_details.get("allowRunner",True) and economics.get("partialPlanned",True) and not pos.partial_taken:
            q=self.broker._partial_close_quantity(pos)
            spec=self.specs.get(intent.pair_id)
            if spec is not None:
                q=float((dec(q)//dec(spec.qty_step))*dec(spec.qty_step))
            if q<=0 or (spec and q<spec.min_order_qty):q=0.0
            preview=self.broker._preview_realize(pos,q,book,reason="partial_take")
            if q>0 and profile.partial_exit=="maker_limit" and preview["net"]>=self.broker._partial_required_net_usd(pos):
                partial=q;result.append(("partial_take",q,"PostOnly",self.broker._partial_limit_price(pos)))
            elif q>0 and profile.partial_exit!="maker_limit" and self.broker._partial_triggered(pos,executable,profile.partial_exit,trade_price=None,trade_notional_usd=None,trade_side=None) and preview["net"]>=self.broker._partial_required_net_usd(pos):
                return [("partial_take",q,"Market",None)]
        if pos.quantity-partial>1e-12:
            if profile.target_exit=="maker_limit":result.append(("runner_target" if pos.partial_taken else "target",pos.quantity-partial,"PostOnly",pos.target))
            elif direction*(executable-pos.target)>=0:result.append(("target",pos.quantity-partial,"Market",None))
        return result


class PairedPortfolio:
    def __init__(self,run,config,arms,emit,*,capacity=32):
        self.run=run;self.config=config;self.arms=arms;self.emit=emit
        self.queues={name:asyncio.Queue(capacity) for name in arms}
        self.reservations={};self.by_symbol={};self.completed=set();self.accepting=False
        self.stop_reason=None;self.tasks=[];self.books={};self.dirty=asyncio.Event()
        self.started_ns=None;self.last_health_ns=0;self.cancel_entries=set();self.exit_reasons={};self.dispatching=set()

    @property
    def balance(self): return min(a.broker.balance for a in self.arms.values())
    @property
    def available_notional(self):
        return max(0,self.balance*self.config.max_leverage-sum(i.plan().notional for i in self.reservations.values()))
    @property
    def available_risk_usd(self):
        return max(0,self.balance*self.config.max_total_risk_fraction-sum(i.plan().expected_net_loss for i in self.reservations.values()))
    @property
    def positions(self):
        # Selection sees an owner if either arm still holds it. No fill copying.
        return {k:v for a in self.arms.values() for k,v in a.broker.positions.items()}
    @property
    def total_pnl(self): return self.balance-self.config.start_balance
    @property
    def total_closed_trades(self): return sum(a.broker.total_closed_trades for a in self.arms.values())
    @property
    def closed_trades(self): return []  # Authoritative separated ledgers are in the experiment report.
    @property
    def total_exposure(self): return sum(i.plan().notional for i in self.reservations.values())
    @property
    def pending_exposure_usd(self): return 0.0
    @property
    def pending_risk_usd(self): return 0.0
    @property
    def open_risk_usd(self): return sum(i.plan().expected_net_loss for i in self.reservations.values())
    @property
    def open_structural_risk_usd(self): return self.open_risk_usd
    @property
    def open_cost_reserve_usd(self): return 0.0
    @property
    def pending_entries(self): return {}
    def cancel_pending(self,symbol,reason):
        pair=self.by_symbol.get(symbol)
        if pair:self.cancel_entries.add(pair)
        return None
    def expire_pending(self,*args): return []
    def can_place_pending(self,symbol): return self.can_open(symbol)
    def can_add(self,plan): return False,"paired occupancy: no new intent before both arms reconcile"
    def can_open(self,symbol):
        if not self.accepting:return False,"paired mode not accepting"
        if symbol in self.by_symbol:return False,"paired symbol still occupied"
        if len(self.reservations)>=self.config.max_open_positions:return False,"shared position limit"
        if any(not a.venue.healthy for a in self.arms.values()):return False,"venue unhealthy"
        if any(q.full() for q in self.queues.values()):return False,"paired queue full"
        return True,"allowed"

    def admit(self,intent,instrument):
        plan=intent.plan();ok,reason=self.can_open(plan.symbol)
        if not ok: return False,reason
        if plan.expected_net_loss>self.available_risk_usd+1e-9 or plan.notional>self.available_notional+1e-9:
            return False,"shared risk/exposure limit"
        if plan.quantity is None or plan.quantity<=0: return False,"explicit equal quantity required"
        if instrument is None or not instrument.tradeable or instrument.min_order_qty<=0 or instrument.min_notional_value<=0:
            return False,"unknown mandatory instrument constraint"
        if plan.expected_net_loss<=0 or plan.notional<=0 or plan.expected_net_loss>self.balance*self.config.max_trade_all_in_loss_fraction:
            return False,"invalid/excessive per-trade risk"
        q=dec(plan.quantity);step=dec(instrument.qty_step)
        if step<=0 or q%step or q<dec(instrument.min_order_qty) or q*dec(plan.market_entry)<dec(instrument.min_notional_value):
            return False,"instrument constraints"
        maximum=instrument.max_market_order_qty if plan.entry_mode!="maker_limit" else instrument.max_order_qty
        if maximum<=0 or q>dec(maximum):return False,"unknown/exceeded instrument maximum"
        # No await between reserve and both put_nowait calls: atomic on engine loop.
        self.reservations[intent.pair_id]=intent;self.by_symbol[plan.symbol]=intent.pair_id
        for name,arm in self.arms.items():
            arm.intents[intent.pair_id]=intent;arm.specs[intent.pair_id]=instrument;self.queues[name].put_nowait(intent)
        self.emit("pair_admitted",asdict(intent))
        return True,"admitted"

    async def dispatch(self,name):
        arm=self.arms[name]
        while True:
            intent=await self.queues[name].get()
            if intent is None:return
            plan=intent.plan()
            self.dispatching.add((name,intent.pair_id))
            if not self.accepting:
                self.emit("entry_not_sent",dict(arm=name,pair_id=intent.pair_id,reason=self.stop_reason));self.dispatching.discard((name,intent.pair_id));continue
            c=arm.command(self.run,intent,qty=plan.quantity,mode="PostOnly" if plan.entry_mode=="maker_limit" else "Market",
                          price=plan.market_entry if plan.entry_mode=="maker_limit" else None)
            try: await arm.venue.submit(c)
            except Exception as exc:arm.venue.fail(type(exc).__name__);self.halt("execution_failure")
            self.dispatching.discard((name,intent.pair_id))
            self.dirty.set()

    def halt(self,reason):
        self.accepting=False;self.stop_reason=self.stop_reason or reason;self.dirty.set()

    async def synchronize(self,arm,intent,desired):
        existing=[o for o in arm.active_orders(intent.pair_id) if o.command.reduce_only]
        wanted={reason:(qty,mode,price) for reason,qty,mode,price in desired}
        for order in existing:
            c=order.command;new=wanted.get(c.reason)
            if new is None or (dec(new[0])!=dec(c.qty)-order.filled or new[1]!=c.mode or (None if new[2] is None else dec(new[2]))!=(None if c.price is None else dec(c.price))):
                await arm.venue.cancel(order)
                if not order.terminal:raise SafetyError("cancel not reconciled; replacement forbidden")
                if c.reason=="partial_take" and order.filled>0:arm.finish_partial(intent.pair_id)
                # Fill may have changed remainder/geometry. Recompute next cycle.
                return
        for reason,qty,mode,price in desired:
            spec=arm.specs.get(intent.pair_id)
            if spec is not None and price is not None:
                price=spec.target_price(price,intent.plan().side)
            if any(o.command.reason==reason for o in arm.active_orders(intent.pair_id)):continue
            if qty<=0:continue
            # A failed protective command halts entries. It is never silently
            # replaced by an order with different semantics to improve fills.
            c=arm.command(self.run,intent,qty=min(qty,float(arm.remaining(intent.plan().symbol))),
                          mode=mode,price=price,reduce=True,reason=reason)
            order=await arm.venue.submit(c)
            if order.status=="Rejected" or order.unknown:
                self.halt("protection_failure")
                return

    async def manage_arm(self,name):
        arm=self.arms[name]
        for intent in list(self.reservations.values()):
            symbol=intent.plan().symbol;book=self.books.get(symbol)
            for order in list(arm.active_orders(intent.pair_id)):
                if not order.command.reduce_only and (self.stop_reason or intent.pair_id in self.cancel_entries
                        or time.perf_counter_ns()-order.command.created_ns>int(self.config.passive_entry_timeout_seconds*1e9)):
                    await arm.venue.cancel(order)
            desired=arm.desired(intent,book,now_ns=time.perf_counter_ns(),
                stop_reason=self.stop_reason or self.exit_reasons.get((name,intent.pair_id)))
            await self.synchronize(arm,intent,desired)

    async def manage_once(self):
        await asyncio.gather(*(self.manage_arm(name) for name in self.arms))

    def release_reconciled(self):
        for pair,intent in list(self.reservations.items()):
            symbol=intent.plan().symbol
            if any(p==pair for _,p in self.dispatching):continue
            if any(arm.remaining(symbol)>0 or arm.active_orders(pair) for arm in self.arms.values()):continue
            if any(any(not arm.venue.orders[k].confirmed for k in arm.orders_by_pair[pair]) for arm in self.arms.values()):continue
            if any(not self.queues[name].empty() for name in self.arms):continue
            # Entries still in a queue or a dispatch await are not terminal.
            if any(not arm.orders_by_pair[pair] for arm in self.arms.values()) and not self.stop_reason:continue
            self.completed.add(pair);del self.reservations[pair];del self.by_symbol[symbol]
            self.emit("pair_reconciled",dict(pair_id=pair))
