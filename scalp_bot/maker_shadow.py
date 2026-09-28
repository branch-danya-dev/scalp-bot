"""Virtual passive candidates; no broker, portfolio reservation or order calls.

Queue evidence is conservative: max(displayed queue, PR60 queue fraction),
aggressor, strict trade-through and public traded quantity. Cancellation never
depletes queue. Markouts are executable-liquidation diagnostics, not spread PnL.
"""
from dataclasses import dataclass, field, asdict
from copy import deepcopy
import math

from .domain import Side
from .paper import PaperBroker


@dataclass
class MakerCandidate:
    identity: str
    symbol: str
    side: str
    epoch: int
    posted_ns: int
    source_sequence: int
    exchange_ms: int
    price: float
    quantity: float
    queue_ahead: float
    expected_spread_bps: float
    remaining: float
    queue_remaining: float
    filled: float = 0
    last_trade_sequence: int = -1
    first_fill_ns: int | None = None
    lots: list = field(default_factory=list)
    cancel_reason: str | None = None
    context: dict | None = None


class MakerShadowEngine:
    WINDOWS_MS = (100, 500, 1000)

    def __init__(self, config, emit, *, ttl_ms=2000, markout_tolerance_ms=100):
        self.config, self.emit = config, emit
        self.ttl_ns = ttl_ms*1_000_000
        self.tolerance_ns = markout_tolerance_ms*1_000_000
        self.active = {}
        self.observing = {}
        self.serial = 0

    def post(self, symbol, side, *, epoch, now_ns, sequence, exchange_ms, book, quantity, depth_fresh, context=None):
        if side not in {"long", "short"} or any(c.symbol == symbol and c.side == side for c in self.observing.values()):
            return None
        if not depth_fresh or not book.bids or not book.asks or not 0 < book.best_bid < book.best_ask:
            return None
        if not math.isfinite(quantity) or quantity <= 0:
            return None
        price, displayed = (book.bids if side == "long" else book.asks)[0]
        queue = max(displayed, quantity*max(0, self.config.maker_queue_ahead_fraction))
        self.serial += 1
        candidate = MakerCandidate(f"maker:{symbol}:{epoch}:{self.serial}", symbol, side, epoch,
            now_ns, sequence, exchange_ms, price, quantity, queue,
            (book.best_ask-book.best_bid)/book.mid*10000, quantity, queue)
        self.active[(symbol, side)] = candidate
        candidate.context = deepcopy(context)
        self.observing[candidate.identity] = candidate
        self.emit("maker_candidate", dict(**asdict(candidate), wouldPostPrice=price,
            queueAheadEstimate=queue, mode="shadow", queueEstimateError=None,
            fillModel="public_trade_strict_through_volume", costAssumption="maker_entry_taker_liquidation"))
        return candidate

    def cancel(self, candidate, reason, now_ns):
        self.active.pop((candidate.symbol, candidate.side), None)
        candidate.cancel_reason = reason
        self.emit("maker_cancel", dict(identity=candidate.identity, symbol=candidate.symbol,
            reason=reason, nowNs=now_ns, filledQuantity=candidate.filled, remaining=candidate.remaining))
        self._finish_if_ready(candidate, now_ns)

    def invalidate(self, symbol, reason, now_ns):
        for candidate in list(self.observing.values()):
            if candidate.symbol != symbol:
                continue
            for lot in candidate.lots:
                for window in self.WINDOWS_MS:
                    lot["markouts"].setdefault(str(window), dict(censor_reason=reason))
            self.cancel(candidate, reason, now_ns)

    def trade(self, symbol, *, epoch, now_ns, sequence, tick_sequence, exchange_ms,
              price, quantity, aggressor, depth_fresh):
        if not depth_fresh:
            self.invalidate(symbol, "missing_depth", now_ns)
            return
        for side in ("long", "short"):
            candidate = self.active.get((symbol, side))
            if candidate is None:
                continue
            if candidate.epoch != epoch:
                self.invalidate(symbol, "reconnect_epoch", now_ns)
                return
            if now_ns-candidate.posted_ns >= self.ttl_ns:
                self.cancel(candidate, "ttl", now_ns)
                continue
            if (sequence <= candidate.source_sequence or tick_sequence <= candidate.last_trade_sequence
                    or exchange_ms <= candidate.exchange_ms or now_ns <= candidate.posted_ns):
                continue  # same received batch / stale evidence cannot fill a later candidate
            candidate.last_trade_sequence = tick_sequence
            sign = 1 if side == "long" else -1
            confirm = max(0, self.config.maker_fill_confirmation_bps)/10000
            # The strict public path excludes PR60's legacy missing-side/volume fallback.
            if (aggressor not in {"Buy", "Sell", "buy", "sell"}
                    or not PaperBroker._maker_entry_aggressor_matches(Side(side), aggressor)
                    or sign*(price-candidate.price*(1-sign*confirm)) >= 0
                    or not math.isfinite(quantity) or quantity <= 0):
                continue
            depleted = min(quantity, candidate.queue_remaining)
            candidate.queue_remaining -= depleted
            filled = min(quantity-depleted, candidate.remaining)
            if filled <= 0:
                continue
            candidate.remaining -= filled
            candidate.filled += filled
            candidate.first_fill_ns = candidate.first_fill_ns or now_ns
            candidate.lots.append(dict(quantity=filled, filledNs=now_ns, sourceSequence=sequence,
                tickSequence=tick_sequence, exchangeMs=exchange_ms, markouts={}, mfeBps=0., maeBps=0.))
            self.emit("maker_fill", dict(identity=candidate.identity, symbol=symbol, side=side,
                quantity=filled, price=candidate.price, nowNs=now_ns,
                sourceSequence=sequence, tickSequence=tick_sequence, partial=candidate.remaining > 1e-12,
                timeToFillMs=(now_ns-candidate.posted_ns)/1e6, queueRemaining=candidate.queue_remaining))
            if candidate.remaining <= 1e-12:
                self.active.pop((symbol, side), None)

    def book(self, symbol, *, epoch, now_ns, book, depth_fresh):
        if not depth_fresh:
            self.invalidate(symbol, "missing_depth", now_ns)
            return
        for candidate in list(self.observing.values()):
            if candidate.symbol != symbol:
                continue
            if candidate.epoch != epoch:
                self.invalidate(symbol, "reconnect_epoch", now_ns)
                return
            if (symbol, candidate.side) in self.active and now_ns-candidate.posted_ns >= self.ttl_ns:
                self.cancel(candidate, "ttl", now_ns)
            for lot in candidate.lots:
                elapsed = now_ns-lot["filledNs"]
                if elapsed <= 0:
                    continue
                sign = 1 if candidate.side == "long" else -1
                price, filled, _ = book.exit_vwap_quantity(Side(candidate.side), lot["quantity"])
                if price is None or filled < lot["quantity"]-1e-12:
                    self.invalidate(symbol, "insufficient_exit_depth", now_ns)
                    break
                gross_bps = sign*(price/candidate.price-1)*10000
                lot["mfeBps"] = max(lot["mfeBps"], gross_bps)
                lot["maeBps"] = max(lot["maeBps"], -gross_bps)
                for window in self.WINDOWS_MS:
                    if str(window) in lot["markouts"] or elapsed < window*1_000_000:
                        continue
                    if elapsed-window*1_000_000 > self.tolerance_ns:
                        value = dict(censor_reason="markout_window_gap")
                    else:
                        exit_price = price*(1-sign*self.config.slippage_bps/10000)
                        gross = sign*(price-candidate.price)*lot["quantity"]
                        fees = lot["quantity"]*(candidate.price*self.config.maker_fee_rate+exit_price*self.config.taker_fee_rate)
                        net = sign*(exit_price-candidate.price)*lot["quantity"]-fees
                        value = dict(censor_reason=None, actualHorizonMs=elapsed/1e6,
                            markoutBps=gross_bps, adverseSelection=gross_bps < 0,
                            grossLiquidationPnl=gross, fees=fees, netAfterCosts=net,
                            grossSpreadCapture=None, quoteBasis="exit_depth_vwap", queueEstimateError=None)
                    lot["markouts"][str(window)] = value
                    self.emit("maker_markout", dict(identity=candidate.identity, symbol=symbol,
                        filledNs=lot["filledNs"], quantity=lot["quantity"], windowMs=window, **value))
            self._finish_if_ready(candidate, now_ns)

    def _finish_if_ready(self, candidate, now_ns):
        if ((candidate.symbol, candidate.side) not in self.active
                and all(len(lot["markouts"]) == len(self.WINDOWS_MS) for lot in candidate.lots)):
            if self.observing.pop(candidate.identity, None) is not None:
                outcome = asdict(candidate)
                outcome.update(completedNs=now_ns, filledQuantity=candidate.filled,
                    filled=candidate.filled > 0, timeToFillMs=(candidate.first_fill_ns-candidate.posted_ns)/1e6
                    if candidate.first_fill_ns is not None else None, portfolioPnl=None)
                self.emit("maker_outcome", outcome)

    def close(self, now_ns):
        for symbol in {c.symbol for c in self.observing.values()}:
            self.invalidate(symbol, "capture_end", now_ns)
