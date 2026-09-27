"""Immutable commands and authoritative fill ledger for the paired experiment."""
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

PROTOCOL = "demo-paper-execution-1h-v1"
TERMINAL = frozenset({"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"})

class SafetyError(RuntimeError):
    pass

class FeeMetadataError(SafetyError):
    pass


def dec(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SafetyError("invalid decimal") from None
    if not result.is_finite():
        raise SafetyError("nonfinite decimal")
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class Intent:
    pair_id: str
    plan_json: str
    source_sequence: int
    observed_ns: int
    author: str
    model_version: str | None = None

    @classmethod
    def freeze(cls, run_id, plan, sequence, observed_ns, author="rule", model_version=None):
        payload = json.dumps(asdict(plan), sort_keys=True, default=str)
        ident = digest([run_id, sequence, observed_ns, payload])[:24]
        return cls(ident, payload, sequence, observed_ns, author, model_version)

    def plan(self):
        from ..domain import Side, TradePlan
        row = json.loads(self.plan_json)
        row["side"] = Side(row["side"])
        return TradePlan(**row)


@dataclass(frozen=True)
class Command:
    link_id: str
    pair_id: str
    symbol: str
    side: str
    qty: str
    mode: str
    price: str | None
    reduce_only: bool
    reason: str
    created_ns: int

    def __post_init__(self):
        if not re.fullmatch(r"dp-[a-f0-9]{32}", self.link_id):
            raise SafetyError("invalid experiment orderLinkId")
        if not re.fullmatch(r"[A-Z0-9]+USDT", self.symbol) or self.side not in ("Buy", "Sell"):
            raise SafetyError("linear USDT command required")
        if dec(self.qty) <= 0 or self.mode not in ("Market", "PostOnly"):
            raise SafetyError("invalid command size/mode")
        if self.mode == "PostOnly" and (self.price is None or dec(self.price) <= 0):
            raise SafetyError("limit price required")

    @classmethod
    def make(cls, run, pair, symbol, side, qty, mode, price, reduce_only, reason, revision, now_ns):
        ident = "dp-" + digest([run, pair, reason, revision])[:32]
        return cls(ident, pair, symbol, side, format(dec(qty),"f"), mode,
                   None if price is None else format(dec(price),"f"), reduce_only, reason, now_ns)

    def payload(self):
        row = dict(category="linear", symbol=self.symbol, side=self.side, qty=self.qty,
                   orderType="Market" if self.mode == "Market" else "Limit",
                   timeInForce="IOC" if self.mode == "Market" else "PostOnly",
                   positionIdx=0, reduceOnly=self.reduce_only, orderLinkId=self.link_id)
        if self.price is not None: row["price"] = self.price
        return row


@dataclass
class Order:
    command: Command
    status: str = "Unsent"
    order_id: str | None = None
    ack_ns: int | None = None
    sent_ns: int | None = None
    first_fill_ns: int | None = None
    final_fill_ns: int | None = None
    cancel_ns: int | None = None
    filled: Decimal = Decimal(0)
    value: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    reported_filled: Decimal = Decimal(0)
    updated_ms: int = -1
    executions: dict = field(default_factory=dict)
    unknown: bool = False
    rejection: str | None = None
    revision_ns: int = 0
    confirmed: bool = False
    fees_known: bool = True

    @property
    def terminal(self):
        return self.status in TERMINAL and not self.unknown and self.filled == self.reported_filled

    @property
    def average(self):
        return self.value / self.filled if self.filled else None

    def update_order(self, row, now_ns):
        if row.get("orderLinkId") != self.command.link_id or row.get("symbol") != self.command.symbol:
            raise SafetyError("foreign order update")
        if row.get("side", self.command.side) != self.command.side:
            raise SafetyError("order side mismatch")
        stamp = int(row.get("updatedTime") or row.get("createdTime") or 0)
        self.reported_filled = max(self.reported_filled, dec(row.get("cumExecQty", "0")))
        if stamp < self.updated_ms: return
        # A stale New acknowledgement must not resurrect a terminal order.
        if self.status in TERMINAL and row["orderStatus"] not in TERMINAL: return
        self.status = row["orderStatus"]
        self.updated_ms = stamp
        self.order_id = row.get("orderId") or self.order_id
        self.ack_ns = self.ack_ns or now_ns
        self.unknown = False
        self.rejection = row.get("rejectReason") or None
        if self.status in TERMINAL: self.cancel_ns = now_ns if self.status != "Filled" else self.cancel_ns

    def fill(self, row, now_ns):
        if row.get("orderLinkId") != self.command.link_id or row.get("symbol") != self.command.symbol:
            raise SafetyError("foreign execution")
        if row.get("execType") != "Trade": raise SafetyError("non-trade execution requires funding ledger")
        ident = row["execId"]
        qty, price, fee = (dec(row[k]) for k in ("execQty", "execPrice", "execFee"))
        currency = row.get("feeCurrency")
        signature = (qty, price, fee, currency, row.get("side"))
        if ident in self.executions:
            if self.executions[ident] != signature: raise SafetyError("conflicting duplicate execution")
            return False
        if qty <= 0 or price <= 0 or row.get("side") != self.command.side:
            raise SafetyError("invalid execution")
        if self.filled + qty > dec(self.command.qty): raise SafetyError("overfill")
        if currency != "USDT" or row.get("extraFees"):
            raise FeeMetadataError("unsupported or unknown execution fee currency/extra fees")
        self.executions[ident] = signature
        self.filled += qty; self.value += qty * price; self.fees += fee
        self.first_fill_ns = self.first_fill_ns or now_ns
        self.final_fill_ns = now_ns
        self.confirmed = False
        return True
