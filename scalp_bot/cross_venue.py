"""Public cross-venue context. Availability clocks, never order authority.

500/1000ms receipt-time windows are preregistered for 100ms snapshot feeds.
Best-level OFI is a snapshot proxy; it cannot recover within-message changes.
Leadership is observed receipt leadership, not proof of exchange causality.
"""
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
import math


@dataclass(frozen=True, slots=True)
class VenueEvent:
    capture_id: str
    venue: str
    symbol: str
    epoch: int
    kind: str
    exchange_ms: int
    receipt_wall_ms: int
    receipt_mono_ns: int
    processing_mono_ns: int
    sequence: int
    bid: float = 0
    ask: float = 0
    bid_qty: float = 0
    ask_qty: float = 0
    price: float = 0
    quantity: float = 0
    side: str = ""
    units_verified: bool = False
    previous_sequence: int | None = None

    def __post_init__(self):
        if (not self.capture_id or not self.symbol or self.venue not in {"bybit", "binance", "okx"}
                or self.kind not in {"quote", "trade"}
                or any(type(v) is not int or v < 0 for v in (self.epoch, self.exchange_ms,
                    self.receipt_wall_ms, self.receipt_mono_ns, self.processing_mono_ns, self.sequence))
                or self.processing_mono_ns < self.receipt_mono_ns):
            raise ValueError("invalid venue provenance")
        if not all(math.isfinite(v) and v >= 0 for v in
                   (self.bid, self.ask, self.bid_qty, self.ask_qty, self.price, self.quantity)):
            raise ValueError("invalid venue price/quantity")
        if self.kind == "quote" and not 0 < self.bid < self.ask:
            raise ValueError("crossed or missing venue quote")
        if self.kind == "trade" and (self.price <= 0 or self.quantity <= 0 or self.side not in {"buy", "sell"}):
            raise ValueError("invalid public trade")


@dataclass
class VenueState:
    epoch: int = 0
    reason: str = "unavailable"
    quotes: deque = field(default_factory=lambda: deque(maxlen=4096))
    trades: deque = field(default_factory=lambda: deque(maxlen=8192))
    last: dict = field(default_factory=dict)


class CrossVenueRuntime:
    WINDOWS_MS = (500, 1000)
    STALE_NS = 1_500_000_000
    BASE_TOLERANCE_NS = 250_000_000

    def __init__(self, capture_id):
        self.capture_id = capture_id
        self.states = defaultdict(VenueState)

    def gap(self, venue, symbol, epoch, reason):
        key = (symbol, venue)
        old = self.states[key]
        if epoch < old.epoch:
            return
        self.states[key] = VenueState(epoch=epoch, reason=reason)

    def ingest(self, event):
        if event.capture_id != self.capture_id:
            raise ValueError("cannot mix capture monotonic clock domains")
        state = self.states[(event.symbol, event.venue)]
        if event.epoch < state.epoch:
            return "old_epoch"
        if event.epoch > state.epoch:
            self.gap(event.venue, event.symbol, event.epoch, "reconnect")
            state = self.states[(event.symbol, event.venue)]
        old = state.last.get(event.kind)
        if old and (event.sequence <= old.sequence or event.exchange_ms < old.exchange_ms
                    or event.receipt_mono_ns < old.receipt_mono_ns
                    or event.processing_mono_ns < old.processing_mono_ns):
            return "out_of_order"
        skew = event.receipt_wall_ms-event.exchange_ms
        if skew < -400 or skew > 2000:
            self.gap(event.venue, event.symbol, event.epoch, "clock_quality")
            return "clock_quality"
        sequence_gap = (event.kind == "quote" and old is not None and event.previous_sequence is not None
            and event.previous_sequence != old.sequence)
        if sequence_gap or (event.kind == "quote" and old and event.receipt_mono_ns-old.receipt_mono_ns > self.STALE_NS):
            self.gap(event.venue, event.symbol, event.epoch, "arrival_gap")
            state = self.states[(event.symbol, event.venue)]
        state.last[event.kind] = event
        if event.kind == "quote":
            state.quotes.append(event)
            state.reason = "available"
        else:
            state.trades.append(event)
        cutoff = event.processing_mono_ns-5_000_000_000
        for rows in (state.quotes, state.trades):
            while rows and rows[0].processing_mono_ns < cutoff:
                rows.popleft()
        return "gap_resnapshot" if sequence_gap else "accepted"

    def _venue(self, symbol, venue, now_ns):
        state = self.states.get((symbol, venue))
        if state is None or not state.quotes:
            return dict(available=False, reason=state.reason if state else "missing")
        # Snapshots may be queried at an earlier as-of time during offline joins.
        quotes = [q for q in state.quotes if q.processing_mono_ns <= now_ns]
        if not quotes:
            return dict(available=False, reason="not_yet_available")
        current = quotes[-1]
        age = now_ns-current.receipt_mono_ns
        if age < 0 or age > self.STALE_NS:
            return dict(available=False, reason="stale")
        mid = (current.bid+current.ask)/2
        returns, baselines = {}, {}
        for window in self.WINDOWS_MS:
            boundary = now_ns-window*1_000_000
            base = next((q for q in reversed(quotes) if q.processing_mono_ns <= boundary), None)
            value = None
            if base is not None and boundary-base.receipt_mono_ns <= self.BASE_TOLERANCE_NS:
                value = (mid/((base.bid+base.ask)/2)-1)*10000
            returns[str(window)] = value
            baselines[str(window)] = base.sequence if value is not None else None
        start = now_ns-1_000_000_000
        trades = [t for t in state.trades if start < t.processing_mono_ns <= now_ns and t.units_verified]
        impulse = sum(t.price*t.quantity*(1 if t.side == "buy" else -1) for t in trades)
        volume = sum(t.price*t.quantity for t in trades)
        ofi = 0.0
        flow_known = current.units_verified and returns["1000"] is not None
        if flow_known:
            for previous, q in zip(quotes, quotes[1:]):
                if q.processing_mono_ns <= start:
                    continue
                ofi += ((q.bid_qty if q.bid >= previous.bid else 0)
                        - (previous.bid_qty if q.bid <= previous.bid else 0))*q.bid
                ofi -= ((q.ask_qty if q.ask <= previous.ask else 0)
                        - (previous.ask_qty if q.ask >= previous.ask else 0))*q.ask
        return dict(available=True, reason="available", mid=mid, ageMs=age/1e6,
            returnsBps=returns, baselineSequences=baselines, source=asdict(current),
            clockQuality=dict(receiptMinusExchangeMs=current.receipt_wall_ms-current.exchange_ms,
                exchangeClockAligned=False, windowDomain="local_processing_availability"),
            ofiUsd=ofi if flow_known else None, ofiScope="best_level_snapshot_proxy",
            tradeImpulse=impulse/volume if volume else None,
            tradeVolumeUsd=volume if trades else None)

    def snapshot(self, symbol, now_ns):
        venues = {v: self._venue(symbol, v, now_ns) for v in ("bybit", "binance", "okx")}
        external = [v for v in ("binance", "okx") if venues[v]["available"]]
        returns = {v: venues[v]["returnsBps"]["500"] for v in external}
        returns = {v: r for v, r in returns.items() if r is not None}
        signs = [0 if abs(r) < .1 else 1 if r > 0 else -1 for r in returns.values()]
        consensus = sum(signs)/len(signs) if signs else None
        bybit = venues["bybit"]
        divergence = {v: (bybit["mid"]/venues[v]["mid"]-1)*10000
                      if bybit["available"] and venues[v]["available"] else None
                      for v in ("binance", "okx")}
        leader = max(returns, key=lambda v: abs(returns[v])) if returns else None
        def agreement(name):
            values = [venues[v].get(name) for v in external]
            values = [x for x in values if x is not None]
            return sum(0 if x == 0 else 1 if x > 0 else -1 for x in values)/len(values) if values else None
        return dict(schema="cross-venue-v1", captureId=self.capture_id, asOfMonoNs=now_ns,
            symbol=symbol, venues=venues, coverage=len(returns),
            consensusDirection=0 if consensus is None or consensus == 0 else 1 if consensus > 0 else -1,
            consensusStrength=abs(consensus) if consensus is not None else None,
            divergenceBps=divergence, leaderVenue=leader,
            leaderMoveBps=returns[leader] if leader else None,
            leaderAgeMs=venues[leader]["ageMs"] if leader else None,
            leaderMeaning="largest_observed_500ms_move_not_exchange_lead_lag",
            ofiConsensus=agreement("ofiUsd"), tradeImpulseConsensus=agreement("tradeImpulse"),
            disagreement=bool(signs and min(signs) < 0 < max(signs)), mode="telemetry_only")


def alignment(context, side):
    if context.get("coverage", 0) != 2 or context.get("consensusStrength") is None:
        return "unavailable"
    direction = context["consensusDirection"]
    if not direction or context["consensusStrength"] < 1:
        return "neutral"
    return "aligned" if direction == (1 if side == "long" else -1) else "opposed"
