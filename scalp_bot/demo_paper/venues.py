"""Independent venues behind the same immutable order interface."""
import asyncio
from dataclasses import asdict, replace
from decimal import Decimal
import time
from .contracts import Order, SafetyError, FeeMetadataError, TERMINAL, dec
from .transport import DemoReject, WriteNotSent, ACCOUNT_SCOPES
from ..domain import Side

class Venue:
    def __init__(self, emit, on_fill):
        self.orders={};self.emit=emit;self.on_fill=on_fill
        self.healthy=True;self.failure=None;self.exec_owner={};self.unknown_fee_raw={}

    def register(self, command):
        if command.link_id in self.orders: raise SafetyError("duplicate command ID")
        order=Order(command, revision_ns=command.created_ns)
        self.orders[command.link_id]=order
        self.emit("command", asdict(command))
        return order

    def execution(self, row):
        order=self.orders.get(row.get("orderLinkId"))
        if order is None: raise SafetyError("foreign execution on dedicated account")
        now=time.perf_counter_ns();ident=row["execId"]
        if ident in self.exec_owner and self.exec_owner[ident]!=order.command.link_id:
            raise SafetyError("execution ID changed ownership")
        physical=row
        try:added=order.fill(row,now)
        except FeeMetadataError:
            # Price/quantity/side/owned ID have already passed strict validation.
            # Preserve physical exposure for reduce-only emergency liquidation;
            # internal fee placeholder is NEVER a confirmed monetary result.
            physical={**row,"execFee":"0","feeCurrency":"USDT","extraFees":"","_fee_unknown":True}
            order.fees_known=False
            self.unknown_fee_raw[ident]=dict(row)
            added=order.fill(physical,now)
            self.fail("execution_fee_metadata_unknown")
        except SafetyError:
            # Repeat of a quarantined execution still uses its original facts.
            if ident not in self.unknown_fee_raw or self.unknown_fee_raw[ident]!=row:raise
            added=False
        if added:
            self.exec_owner[ident]=order.command.link_id
            self.emit("fill",dict(pair_id=order.command.pair_id, received_ns=now,fees_known=order.fees_known, **row))
            self.on_fill(order,physical,now)

    def order_update(self,row):
        order=self.orders.get(row.get("orderLinkId"))
        if order is None: raise SafetyError("foreign order on dedicated account")
        order.update_order(row,time.perf_counter_ns())
        self.emit("order",dict(pair_id=order.command.pair_id, **row))

    def fail(self,reason):
        self.healthy=False;self.failure=reason
        self.emit("venue_failure",dict(reason=reason))


class DemoVenue(Venue):
    def __init__(self, rest, emit, on_fill):
        super().__init__(emit,on_fill);self.rest=rest;self.position_rows={}
        self.last_reconcile_ns=0;self.final_verified={}

    async def submit(self, command):
        order=self.register(command);order.status="Submitting"
        try:
            result=await self.rest.request("POST","/v5/order/create",command.payload())
            order.order_id=result["orderId"];order.ack_ns=time.perf_counter_ns()
            if order.status=="Submitting": order.status="Acknowledged"
            self.emit("ack",dict(link_id=command.link_id,pair_id=command.pair_id,ns=order.ack_ns))
        except WriteNotSent:
            order.status="Rejected";order.rejection="local_admission_not_sent"
            order.confirmed=True;order.create_rejected=True
            self.emit("entry_not_sent",dict(link_id=command.link_id,pair_id=command.pair_id))
            return order
        except DemoReject as exc:
            # Codes for duplicate/client request cannot establish absence.
            if exc.code in (10000,10014,10016,10019,110072): order.unknown=True
            else:
                order.status="Rejected";order.rejection=str(exc);order.confirmed=True;order.create_rejected=True
                self.emit("rejected",dict(link_id=command.link_id,code=exc.code))
                return order
        except SafetyError:
            order.unknown=True
        await self.reconcile_order(order)
        return order

    async def reconcile_order(self,order):
        # An explicit create rejection has no exchange order to look up.
        # Transport/duplicate/uncertain failures never set this flag.
        if order.create_rejected and order.terminal and order.confirmed:return True
        params=dict(category="linear",symbol=order.command.symbol,orderLinkId=order.command.link_id)
        rows=await self.rest.pages("/v5/order/realtime",params)
        if not rows: rows=await self.rest.pages("/v5/order/history",params)
        if not rows:
            order.unknown=True
            self.fail("order_presence_unknown")
            return False
        matching=[r for r in rows if r.get("orderLinkId")==order.command.link_id]
        if len(matching)!=1: raise SafetyError("ambiguous order identity")
        self.order_update(matching[0])
        fills=await self.rest.pages("/v5/execution/list",params)
        for row in sorted(fills,key=lambda r:(int(r["execTime"]),r["execId"])):
            if row.get("execType")=="Trade": self.execution(row)
        order.confirmed=order.filled==order.reported_filled
        if not order.confirmed: order.unknown=True
        self.emit("order_reconciled",dict(link_id=order.command.link_id,terminal=order.terminal,
            confirmed=order.confirmed,filled=str(order.filled),reported=str(order.reported_filled)))
        return order.confirmed

    async def cancel(self, order):
        if order.terminal: return
        try:
            await self.rest.request("POST","/v5/order/cancel",dict(category="linear",
                symbol=order.command.symbol,orderLinkId=order.command.link_id))
        except SafetyError:
            order.unknown=True
        # A cancel ack is not a cancel; racing fills are collected here.
        await self.reconcile_order(order)

    async def amend(self, order, *, qty, price):
        if order.terminal: return
        if dec(qty)<order.filled: raise SafetyError("amend below already filled size")
        try:
            await self.rest.request("POST","/v5/order/amend",dict(category="linear",
                symbol=order.command.symbol,orderLinkId=order.command.link_id,qty=str(qty),price=str(price)))
        except SafetyError:
            order.unknown=True
        rows=await self.rest.pages("/v5/order/realtime",dict(category="linear",symbol=order.command.symbol,
                                                         orderLinkId=order.command.link_id))
        if len(rows)!=1: self.fail("amend_unknown");return
        row=rows[0]
        order.command=replace(order.command,qty=str(row["qty"]),price=str(row["price"]))
        order.revision_ns=time.perf_counter_ns()
        await self.reconcile_order(order)

    async def reconcile(self,*,force=False):
        for order in list(self.orders.values()):
            key=order.command.link_id
            fingerprint=(order.status,order.filled,order.reported_filled,order.updated_ms,len(order.executions))
            needs_final=force and self.final_verified.get(key)!=fingerprint
            if needs_final or not order.terminal or not order.confirmed:
                await self.reconcile_order(order)
                if force and order.terminal and order.confirmed:
                    self.final_verified[key]=(order.status,order.filled,order.reported_filled,order.updated_ms,len(order.executions))
        # Completed per-order checks survive the bounded final-pass timeout.
        # Every pass still queries all account scopes for open orders/positions.
        # A private gap can hide activity anywhere on the dedicated account.
        # Inspect every supported scope; never cancel or close foreign work.
        own_rows=[]
        for category,settle_coin in ACCOUNT_SCOPES:
            params=dict(category=category)
            if settle_coin:params["settleCoin"]=settle_coin
            own_scope=category=="linear" and settle_coin=="USDT"
            opened=await self.rest.pages("/v5/order/realtime",dict(params,openOnly=0))
            if any(not own_scope or r.get("orderLinkId") not in self.orders for r in opened):
                raise SafetyError("foreign open order detected")
            if category=="spot":continue
            rows=await self.rest.pages("/v5/position/list",params)
            if not own_scope and any(dec(r["size"])!=0 for r in rows):
                raise SafetyError("foreign position detected")
            if own_scope:
                if any(int(r["positionIdx"])!=0 for r in rows):raise SafetyError("hedge mode forbidden")
                own_rows=rows
        self.position_rows={r["symbol"]:r for r in own_rows if dec(r["size"])!=0}
        self.last_reconcile_ns=time.perf_counter_ns()
        return self.position_rows

    async def message(self,message):
        topic=message.get("topic")
        if topic not in ("order","execution","position"): return
        for row in message.get("data",[]):
            if row.get("category","linear")!="linear": raise SafetyError("foreign account activity")
            if topic=="order": self.order_update(row)
            elif topic=="execution":
                if row.get("execType")=="Trade": self.execution(row)
                else: self.emit("nontrade_execution",row)  # Funding comes from the transaction ledger once.
            else:
                if dec(row["size"])!=0 and not any(o.command.symbol==row["symbol"] for o in self.orders.values()):
                    raise SafetyError("foreign position observation")
                if int(row["positionIdx"])!=0:raise SafetyError("hedge mode forbidden")
                self.position_rows[row["symbol"]]=row
                self.emit("position_observation",{k:row.get(k) for k in ("symbol","side","size","positionIdx","seq")})


class PaperVenue(Venue):
    """Original depth/slippage and queue assumptions, with explicit resting orders.

    Matching never reads Demo results. Tape must have arrived strictly after the
    order or amendment; one batch cannot be recycled into later-order evidence.
    """
    def __init__(self,config,emit,on_fill):
        super().__init__(emit,on_fill);self.config=config;self.books={};self.tape={};self.counter=0

    async def submit(self,command):
        order=self.register(command);order.sent_ns=order.ack_ns=time.perf_counter_ns()
        order.status="New"
        book=self.books.get(command.symbol)
        if book is None or not book.best_bid or not book.best_ask:
            order.status="Rejected";order.rejection="book_unavailable";order.confirmed=True;return order
        if command.mode=="PostOnly":
            crosses=dec(command.price)>=dec(book.best_ask) if command.side=="Buy" else dec(command.price)<=dec(book.best_bid)
            if crosses:
                order.status="Rejected";order.rejection="post_only_would_cross";order.confirmed=True
        else:
            side=Side.LONG if command.side=="Buy" else Side.SHORT
            price,visible,_=book.entry_vwap_quantity(side,float(command.qty))
            qty=min(dec(command.qty),dec(visible))
            if price and qty>0:
                slip=self.config.slippage_bps/10000
                price=price*(1+slip if command.side=="Buy" else 1-slip)
                self._fill(order,qty,dec(price),False)
            order.status="Filled" if order.filled==dec(command.qty) else "PartiallyFilledCanceled"
        order.reported_filled=order.filled;order.confirmed=True
        self.emit("paper_order",dict(link_id=command.link_id,status=order.status,rejection=order.rejection))
        return order

    def _fill(self,order,qty,price,maker):
        if order.command.reduce_only:
            owned=getattr(self,"remaining",lambda symbol:Decimal(0))(order.command.symbol)
            qty=min(qty,owned)
        if qty<=0: return
        self.counter+=1
        fee=qty*price*dec(self.config.maker_fee_rate if maker else self.config.taker_fee_rate)
        self.execution(dict(orderLinkId=order.command.link_id,symbol=order.command.symbol,side=order.command.side,
            execType="Trade",execId="paper-"+str(self.counter),execQty=str(qty),execPrice=str(price),
            execFee=str(fee),feeCurrency="USDT",isMaker=maker,execTime=str(int(time.time()*1000))))
        order.reported_filled=order.filled
        if order.filled==dec(order.command.qty): order.status="Filled"
        else: order.status="PartiallyFilled"
        order.confirmed=True

    def market(self,symbol,book,*,trade_price=None,trade_notional=None,trade_side=None,receipt_ns=None):
        self.books[symbol]=book
        for order in list(self.orders.values()):
            c=order.command
            if c.symbol!=symbol or c.mode!="PostOnly" or order.status not in ("New","PartiallyFilled"): continue
            if receipt_ns is None or receipt_ns<=max(order.revision_ns,order.sent_ns or c.created_ns): continue
            if trade_price is None or trade_notional is None: continue
            limit=dec(c.price); confirm=dec(self.config.maker_fill_confirmation_bps)/10000
            through=dec(trade_price)<=limit*(1-confirm) if c.side=="Buy" else dec(trade_price)>=limit*(1+confirm)
            if not through or trade_side!=("Sell" if c.side=="Buy" else "Buy"): continue
            total=self.tape.get(c.link_id,Decimal(0))+dec(trade_notional)
            self.tape[c.link_id]=total
            required=(dec(c.qty)-order.filled)*limit*(1+dec(self.config.maker_queue_ahead_fraction))
            if total>=required:
                self._fill(order,dec(c.qty)-order.filled,limit,True)
                self.tape[c.link_id]=Decimal(0)

    async def cancel(self,order):
        if not order.terminal:
            order.status="Cancelled";order.cancel_ns=time.perf_counter_ns();order.confirmed=True

    async def amend(self,order,*,qty,price):
        if dec(qty)<order.filled: raise SafetyError("amend below filled size")
        order.command=replace(order.command,qty=str(qty),price=str(price));order.revision_ns=time.perf_counter_ns()
        self.tape.pop(order.command.link_id,None)

    async def reconcile(self):
        return {}
