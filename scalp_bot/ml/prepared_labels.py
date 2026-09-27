"""Executable frozen structural labels using the unchanged PaperBroker.

Independent candidate paths are research observations, never portfolio returns.
The caller must deliver each causal tick and the ordinary context evaluations.
Missing transport/depth/context coverage censors the path, never fabricates 0R.
"""
from copy import deepcopy
from dataclasses import dataclass, asdict
import hashlib
import json

from ..domain import TradePlan, Side
from ..execution import fee_rate_for_details
from ..execution_book import coherent_execution_book
from ..paper import PaperBroker
from ..runtime_clock import ReplayRuntimeClock
from ..strategy import create_default_strategies


@dataclass(frozen=True)
class LabelPolicy:
    entry_latency_ms: int = 250
    horizon_ms: int = 180_000
    max_depth_age_ms: int = 1500
    max_pending: int = 128

    def __post_init__(self):
        if self.entry_latency_ms < 0 or self.horizon_ms <= self.entry_latency_ms or self.max_depth_age_ms <= 0 or self.max_pending <= 0:
            raise ValueError("invalid label policy")

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class PreparedPath:
    def __init__(self, prepared, config, policy):
        self.prepared = deepcopy(prepared)
        self.row = self.prepared["row"]
        raw = dict(self.prepared["frozenPlan"])
        raw["side"] = Side(raw["side"])
        self.plan = TradePlan(**raw)
        self.policy = policy
        self.start_ns = self.row["source"]["available_mono_ns"]
        self.start_wall_ms = self.row["available_wall_ms"]
        if self.start_wall_ms is None:
            raise ValueError("global wall-time provenance required")
        self.clock = ReplayRuntimeClock(wall_seconds=self.start_wall_ms/1000, mono_ns=self.start_ns)
        self.broker = PaperBroker(config, clock=self.clock)
        self.strategies = {s.key: s for s in create_default_strategies()}
        self.broker.position_manager = lambda pos, gross: self.strategies[pos.strategy].manage_progress(config, self.clock, pos, gross)
        self.entered_ns = None
        self.submitted_sequence = None
        self.filled_sequence = None
        self.filled_exchange_ms = None
        self.finished = None
        self.last_ns = self.start_ns
        self.events = []
        self.mfe_r = self.mae_r = 0.0
        self.mfe_ns = None
        self.first_target_ns = self.first_stop_ns = None

    def _time(self, now_ns, wall_ms):
        if now_ns < self.last_ns:
            raise ValueError("label processing order regressed")
        # Wall clock steps would compromise global splits; never stitch domains.
        if abs(wall_ms-(self.start_wall_ms+(now_ns-self.start_ns)/1e6)) > 400:
            return self.censor("local_wall_clock_step", now_ns, wall_ms)
        self.clock.set_observation(wall_seconds=wall_ms/1000, mono_ns=now_ns)
        self.last_ns = now_ns
        return None

    def censor(self, reason, now_ns, wall_ms):
        if self.finished is None:
            self.finished = self._result(now_ns, wall_ms, censor_reason=reason)
        return self.finished

    def _result(self, now_ns, wall_ms, *, censor_reason=None, trade=None):
        trade = trade or {}
        risk = trade.get("initialRiskUsd")
        net = trade.get("netPnl")
        reason = trade.get("reason", "")
        return dict(identity=self.row["identity"], source=self.row["source"], symbol=self.plan.symbol,
            strategy=self.plan.strategy, side=self.plan.side.value, segment=self.prepared["segment"],
            capture_id=self.row["source"]["capture_id"],
            episode=self.plan.symbol+":"+self.plan.strategy+":"+(self.row["intent"].get("episode_key")
                or self.row["source"]["capture_id"]+":"+self.row["identity"]),
            features=self.row["features"], crossVenue=self.prepared["crossVenue"],
            crossVenueAlignment=self.prepared["crossVenueAlignment"],
            available_wall_ms=self.start_wall_ms, label_end_wall_ms=wall_ms, label_end_mono_ns=now_ns,
            censor_reason=censor_reason, executable=censor_reason is None,
            target_before_stop=(reason in {"target", "runner_target"}) if censor_reason is None else None,
            realized_net_r=net/risk if censor_reason is None and risk and net is not None else None,
            netPnl=net if censor_reason is None else None, initialRiskUsd=risk,
            executable_mfe_r=self.mfe_r if censor_reason is None else None,
            executable_mae_r=self.mae_r if censor_reason is None else None,
            time_to_target_ms=(self.first_target_ns-self.entered_ns)/1e6 if self.first_target_ns is not None else None,
            time_to_stop_ms=(self.first_stop_ns-self.entered_ns)/1e6 if self.first_stop_ns is not None else None,
            time_to_mfe_ms=(self.mfe_ns-self.entered_ns)/1e6 if self.mfe_ns is not None else None,
            no_follow_through=("no_follow" in reason) if censor_reason is None else None,
            target=reason in {"target", "runner_target"} if censor_reason is None else None,
            partial=trade.get("partialTaken") if censor_reason is None else None,
            stop=reason == "stop" if censor_reason is None else None,
            exit_reason=reason or None, policyHash=self.policy.digest,
            cost_fill_provenance=dict(executor="PaperBroker", events=self.events, trade=trade,
                frozenPlan=self.prepared["frozenPlan"], entryLatencyMs=self.policy.entry_latency_ms,
                entrySequence=self.submitted_sequence, scope="independent_label_not_portfolio"),
            trainingReady=censor_reason is None and bool(risk), portfolioPnl=None)

    def _events(self, events, now_ns, wall_ms):
        for event in events:
            self.events.append(dict(nowNs=now_ns, wallMs=wall_ms, event=deepcopy(event)))
            if event.get("event") == "entry_filled":
                self.entered_ns = now_ns
            if event.get("event") == "entry_cancelled":
                return self.censor("not_filled:"+event["reason"], now_ns, wall_ms)
            if event.get("event") == "trade_closed":
                if event["reason"] in {"target", "runner_target"}:
                    self.first_target_ns = now_ns
                if event["reason"] == "stop":
                    self.first_stop_ns = now_ns
                self.finished = self._result(now_ns, wall_ms, trade=event)
        return self.finished

    def advance(self, *, now_ns, wall_ms, sequence, epoch, book, depth, fresh,
                exchange_ms, tick=None, funding=None):
        if self.finished is not None:
            return self.finished
        if self._time(now_ns, wall_ms):
            return self.finished
        if epoch != self.prepared["epoch"]:
            return self.censor("transport_epoch", now_ns, wall_ms)
        if not fresh or not depth.bids or not depth.asks:
            return self.censor("depth_gap", now_ns, wall_ms)
        if now_ns-self.start_ns > self.policy.horizon_ms*1_000_000:
            return self.censor("right_censored_horizon", now_ns, wall_ms)
        if sequence <= self.row["source"]["source_sequence"] or now_ns-self.start_ns < self.policy.entry_latency_ms*1_000_000:
            return None
        symbol = self.plan.symbol
        executable = coherent_execution_book(book, depth)
        if self.submitted_sequence is None:
            quantity = self.plan.quantity or self.plan.notional/self.plan.market_entry
            price, available, _ = executable.entry_vwap_quantity(self.plan.side, quantity)
            if price is None or available < quantity-1e-10:
                return self.censor("entry_depth", now_ns, wall_ms)
            self.submitted_sequence = sequence
            try:
                if self.plan.entry_mode == "maker_limit":
                    self.broker.place_pending(self.plan, min_trade_ts_ms=exchange_ms)
                else:
                    pos = self.broker.open(self.plan, executable)
                    self.entered_ns = now_ns
                    self.filled_sequence = sequence
                    self.filled_exchange_ms = exchange_ms
                    self.events.append(dict(nowNs=now_ns, wallMs=wall_ms, event=dict(event="entry_filled", position=pos.public())))
            except RuntimeError as exc:
                return self.censor("entry_rejected:"+str(exc), now_ns, wall_ms)
            return None  # nothing from the event causing placement fills it retroactively
        kwargs = {}
        if funding:
            kwargs = dict(funding_rate=funding.get("fundingRate"), funding_time_ms=funding.get("nextFundingTimeMs"),
                funding_mark_price=funding.get("markPrice"))
        if symbol in self.broker.pending_entries:
            if tick is None or sequence <= self.submitted_sequence or tick.side not in {"Buy", "Sell"}:
                return self._events(self.broker.expire_pending(wall_ms/1000), now_ns, wall_ms)
            events = self.broker.mark_pending(symbol, tick.price, trade_ts_ms=tick.ts_ms,
                trade_notional_usd=tick.notional, trade_side=tick.side, observed_at_ms=exchange_ms, **kwargs)
            if self._events(events, now_ns, wall_ms):
                return self.finished
            if self.entered_ns == now_ns:
                self.filled_sequence = sequence
                self.filled_exchange_ms = tick.ts_ms
                return None  # same public batch cannot become maker-exit evidence
        pos = self.broker.positions.get(symbol)
        if pos is None:
            return None
        price, available, _ = executable.exit_vwap_quantity(pos.side, pos.quantity)
        if price is None or available < pos.quantity-1e-10:
            return self.censor("exit_depth", now_ns, wall_ms)
        sign = 1 if pos.side == Side.LONG else -1
        exit_price = price*(1-sign*self.broker.config.slippage_bps/10000)
        net = (pos.realized_net_usd+sign*(exit_price-pos.entry)*pos.quantity-pos.entry_fee_remaining
               -pos.quantity*exit_price*fee_rate_for_details(self.broker.config, "taker_market", pos.strategy_details))
        value = net/pos.initial_risk_usd if pos.initial_risk_usd else 0
        if value > self.mfe_r:
            self.mfe_r, self.mfe_ns = value, now_ns
        self.mae_r = max(self.mae_r, -value)
        causal_tick = (tick is not None and sequence > self.filled_sequence
            and tick.ts_ms > self.filled_exchange_ms)
        events = self.broker.mark(symbol, book.mid, book, depth_book=depth,
            trade_price=tick.price if causal_tick else None,
            trade_notional_usd=tick.notional if tick is not None else None,
            trade_side=tick.side if tick is not None else None, observed_at_ms=exchange_ms, **kwargs)
        return self._events(events, now_ns, wall_ms)

    def context(self, session, *, now_ns, wall_ms, clock_valid, observed_ms):
        if self.finished is not None:
            return self.finished
        if self._time(now_ns, wall_ms):
            return self.finished
        if not clock_valid or session.market_context is None:
            return self.censor("context_clock_gap", now_ns, wall_ms)
        pos = self.broker.positions.get(self.plan.symbol)
        if pos is None:
            return None
        if not session.deep_book_is_fresh() or not session.book_is_fresh():
            return self.censor("context_depth_gap", now_ns, wall_ms)
        strategy = self.strategies[pos.strategy]
        reason = strategy.manage_position(side=pos.side, unrealized_pnl=pos.unrealized_pnl,
            opened_at=pos.opened_at, strategy_details=pos.strategy_details,
            decision=session.decisions.get(pos.strategy), trend=session.trend, last_price=session.last_price,
            book=session.orderbook, market_context=session.market_context, observed_at_ms=observed_ms)
        if reason:
            return self._events([self.broker.close(pos.symbol, session.orderbook, reason,
                depth_book=session.depth_orderbook())], now_ns, wall_ms)
        return None


class PreparedLabelEngine:
    def __init__(self, config, emit, policy=None):
        self.config, self.emit = config, emit
        self.policy = policy or LabelPolicy()
        self.pending = {}

    def add(self, prepared):
        row = prepared["row"]
        if not prepared["economicsAllowed"]:
            self.emit("prepared_censored", dict(identity=row["identity"], source=row["source"],
                censor_reason="economics_rejected:"+prepared["economicReason"], realized_net_r=None))
            return
        path = PreparedPath(prepared, self.config, self.policy)
        if len(self.pending) >= self.policy.max_pending:
            self.emit("prepared_label", path.censor("label_capacity", path.start_ns, path.start_wall_ms))
        else:
            self.pending[row["identity"]] = path

    def market(self, symbol, **kwargs):
        for identity, path in list(self.pending.items()):
            if path.plan.symbol == symbol:
                self._done(identity, path.advance(**kwargs))

    def context(self, session, **kwargs):
        for identity, path in list(self.pending.items()):
            if path.plan.symbol == session.symbol:
                self._done(identity, path.context(session, **kwargs))

    def invalidate(self, symbol, reason, now_ns, wall_ms):
        for identity, path in list(self.pending.items()):
            if path.plan.symbol == symbol:
                self._done(identity, path.censor(reason, now_ns, wall_ms))

    def _done(self, identity, result):
        if result is not None:
            self.emit("prepared_label", result)
            self.pending.pop(identity, None)
