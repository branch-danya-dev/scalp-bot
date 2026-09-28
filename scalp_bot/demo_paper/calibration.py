"""Offline execution calibration using the existing immutable intent/order ledger.

There is deliberately no network venue, transport, credential or launch method.
The future owner-authorized runner can supply events from PaperVenue/DemoVenue.
"""
from copy import deepcopy
from dataclasses import asdict

from .contracts import Command, Intent, Order, SafetyError, dec, digest
from ..pipeline_evidence import quantiles


class DemoExecutionCalibration:
    def __init__(self, *, run_id, account_hash, instrument_hash):
        if not run_id or any(len(h)!=64 or any(c not in "0123456789abcdef" for c in h)
                             for h in (account_hash,instrument_hash)):
            raise SafetyError("exact account/instrument hashes required")
        self.run_id, self.account_hash, self.instrument_hash = run_id, account_hash, instrument_hash
        self.intents, self.orders, self.events, self.fills = {}, {"paper":{}, "demo":{}}, [], []
        self.failure = None
        self.reconciled = {"paper":False,"demo":False}

    def freeze(self, intent, instrument):
        if not isinstance(intent, Intent) or intent.pair_id in self.intents:
            raise SafetyError("unique frozen causal Intent required")
        plan=intent.plan()
        if instrument.symbol != plan.symbol or not plan.quantity or plan.quantity <= 0:
            raise SafetyError("normalized explicit quantity required")
        normalized=instrument.normalize_quantity(entry_price=plan.market_entry,
            requested_notional=plan.quantity*plan.market_entry, market_order=plan.entry_mode!="maker_limit")
        if normalized is None or abs(normalized[0]-plan.quantity)>1e-10:
            raise SafetyError("quantity violates frozen instrument constraints")
        self.intents[intent.pair_id]=dict(intent=asdict(intent), instrument=asdict(instrument),
            planHash=digest(asdict(plan)), geometry=[plan.symbol,plan.side.value,plan.quantity,plan.market_entry,plan.stop,plan.target])

    def sent(self, arm, command, now_ns):
        if arm not in self.orders or not isinstance(command,Command) or command.pair_id not in self.intents:
            raise SafetyError("unknown calibration arm/intent")
        if command.link_id in self.orders[arm] or now_ns < command.created_ns:
            raise SafetyError("duplicate command or regressed send time")
        frozen=self.intents[command.pair_id]
        symbol, side, qty, entry, stop, target=frozen["geometry"]
        plan=Intent(**frozen["intent"]).plan()
        expected_mode="PostOnly" if plan.entry_mode=="maker_limit" else "Market"
        if not command.reduce_only and (command.mode!=expected_mode or
                (command.mode=="PostOnly" and dec(command.price)!=dec(plan.setup_entry)) or
                (command.mode=="Market" and command.price is not None)):
            raise SafetyError("command execution geometry differs from frozen plan")
        if dec(command.qty)%dec(frozen["instrument"]["qty_step"]) != 0:
            raise SafetyError("command quantity precision differs from frozen instrument")
        if command.price is not None and dec(command.price)%dec(frozen["instrument"]["tick_size"]) != 0:
            raise SafetyError("command price precision differs from frozen instrument")
        if command.symbol!=symbol or (not command.reduce_only and
                (dec(command.qty)!=dec(qty) or command.side!=("Buy" if side=="long" else "Sell"))):
            raise SafetyError("command differs from frozen entry")
        if command.reduce_only and (command.side==("Buy" if side=="long" else "Sell") or dec(command.qty)>dec(qty)):
            raise SafetyError("protective command increases exposure")
        order=Order(command,sent_ns=now_ns,revision_ns=now_ns,status="Submitting")
        self.orders[arm][command.link_id]=order
        self.reconciled[arm]=False
        self.events.append(dict(kind="sent",arm=arm,now_ns=now_ns,command=asdict(command)))

    def ack(self,arm,link_id,now_ns):
        order=self._order(arm,link_id)
        if now_ns<order.sent_ns:
            raise SafetyError("ack precedes send")
        order.ack_ns=order.ack_ns or now_ns
        self.events.append(dict(kind="ack",arm=arm,link_id=link_id,now_ns=now_ns))

    def _order(self,arm,link):
        if arm not in self.orders or link not in self.orders[arm]:
            self.failure="foreign_account_activity"
            raise SafetyError(self.failure)
        return self.orders[arm][link]

    def execution(self,arm,row,now_ns):
        order=self._order(arm,row.get("orderLinkId"))
        if now_ns<order.sent_ns or type(row.get("isMaker")) is not bool:
            raise SafetyError("missing maker/taker or invalid fill time")
        if order.fill(row,now_ns):
            self.fills.append(dict(arm=arm,received_ns=now_ns,pair_id=order.command.pair_id,**deepcopy(row)))
            self.reconciled[arm]=False

    def order_update(self,arm,row,now_ns):
        order=self._order(arm,row.get("orderLinkId"))
        order.update_order(row,now_ns)
        self.events.append(dict(kind="order",arm=arm,now_ns=now_ns,row=deepcopy(row)))

    def operation(self,arm,link,kind,now_ns,**details):
        self._order(arm,link)
        if kind not in {"cancel_requested","cancel_ack","amend_requested","amend_ack","stop","target","heartbeat_gap"}:
            raise SafetyError("unknown calibration operation")
        if kind=="heartbeat_gap":self.failure="private_ws_gap"
        self.reconciled[arm]=False
        self.events.append(dict(kind=kind,arm=arm,link_id=link,now_ns=now_ns,details=deepcopy(details)))

    def reconcile(self,arm,*,account_hash,instrument_hash,foreign_activity,unknown_exposure,positions_flat):
        if arm not in self.orders:raise SafetyError("unknown arm")
        if account_hash!=self.account_hash or instrument_hash!=self.instrument_hash or foreign_activity:
            self.failure="account_or_instrument_mismatch"
            raise SafetyError(self.failure)
        clean=(bool(self.orders[arm]) and not self.failure and not unknown_exposure and positions_flat and all(o.terminal and o.fees_known
            for o in self.orders[arm].values()))
        self.reconciled[arm]=clean
        self.events.append(dict(kind="reconciliation",arm=arm,clean=clean,
            unknown_exposure=unknown_exposure,positions_flat=positions_flat))
        return clean

    def report(self):
        timing={arm:{name:[] for name in ("order_to_ack_ms","order_to_first_fill_ms","order_to_final_fill_ms")}
            for arm in self.orders}
        ledgers={}
        for arm,orders in self.orders.items():
            ledgers[arm]=[]
            for order in orders.values():
                for name,stamp in (("order_to_ack_ms",order.ack_ns),("order_to_first_fill_ms",order.first_fill_ns),
                        ("order_to_final_fill_ms",order.final_fill_ns if order.filled==dec(order.command.qty) else None)):
                    if stamp is not None:timing[arm][name].append((stamp-order.sent_ns)/1e6)
                ledgers[arm].append(dict(pair_id=order.command.pair_id,link_id=order.command.link_id,
                    reason=order.command.reason,reduceOnly=order.command.reduce_only,status=order.status,
                    filled=str(order.filled),remaining=str(dec(order.command.qty)-order.filled),
                    averagePrice=str(order.average) if order.average is not None else None,actualFees=str(order.fees),
                    terminal=order.terminal,feesKnown=order.fees_known))
        comparisons=[]
        for pair_id in self.intents:
            arms={arm:[o for o in orders.values() if o.command.pair_id==pair_id and not o.command.reduce_only]
                for arm,orders in self.orders.items()}
            if all(len(v)==1 for v in arms.values()):
                a,b=arms["paper"][0],arms["demo"][0]
                comparisons.append(dict(pair_id=pair_id,quantityDifference=str(b.filled-a.filled),
                    feeDifference=str(b.fees-a.fees),fillPriceDifference=str(b.average-a.average)
                        if a.average is not None and b.average is not None else None))
        return dict(schema="demo-execution-calibration-v1",executionAuthorized=False,
            scope="offline_contract_not_live_qualification",status="NOT_MET" if self.failure else
                "CONTRACT_COMPLETE" if all(self.reconciled.values()) else "INCONCLUSIVE",
            failure=self.failure,reconciled=dict(self.reconciled),accountHash=self.account_hash,
            instrumentHash=self.instrument_hash,intents=deepcopy(self.intents),ledgers=ledgers,
            comparisons=comparisons,timing={a:{k:quantiles(v) for k,v in s.items()} for a,s in timing.items()},
            events=deepcopy(self.events),fills=deepcopy(self.fills),promotionAuthorized=False,
            liveQualification="NOT_TESTED")
