"""Independent preparation, one synchronous admission/execution owner.

ScenarioRouter remains the per-strategy lifecycle primitive and legacy reader.
Shared applicability/episodes are evaluated once per market observation.
"""
from copy import deepcopy
from dataclasses import replace
from math import isfinite
from .domain import StrategyDecision, Action

from .scenario import ScenarioRouter, TERMINAL, ROLES

BUSY = {"RESERVING", "ORDER_PENDING", "PARTIAL_FILL", "IN_POSITION", "CANCELLING", "RECONCILING"}
PRIORITY = tuple(key for key in ROLES if key != "orderbook_density")


class ParallelScenarioRouter(ScenarioRouter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.children = {key: ScenarioRouter(**kwargs) for key in PRIORITY}
        for key, child in self.children.items():
            child.identity_owner = key
        self.executions = {}
        self.epochs = {}
        self._plans = {}

    def transition(self, s, state, now, reason):
        child = self.children.get(s.owner)
        if child is not None:
            child.transition(s, state, now, reason)
        else:
            super().transition(s, state, now, reason)

    def scenario_for(self, symbol, strategy):
        legacy = self.scenarios.get(symbol)
        # Explicit fixtures and restored legacy contracts retain their owner.
        if legacy is not None and legacy.owner == strategy:
            return legacy
        child = self.children.get(strategy)
        return child.scenarios.get(symbol) if child else None

    def all_for(self, symbol):
        return {key: s for key in PRIORITY if (s := self.scenario_for(symbol, key)) is not None}

    def observe(self, symbol, context, candles, structure, enabled, now, *, position=None, pending=None):
        assessment = self.assess_observation(symbol, context, candles, structure, enabled)
        execution = self.executions.get(symbol)
        if position is not None or pending is not None:
            plan = position if position is not None else pending.plan
            execution = self.restore_execution(symbol, plan, now)
            self.transition(execution, "IN_POSITION" if position is not None else "ORDER_PENDING", now,
                            "execution ownership is immutable")
            if pending is not None and not enabled.get(execution.owner, False):
                execution.cancellation = "strategy_disabled"
        previous = self.scenarios.get(symbol)
        for key, child in self.children.items():
            old = self.scenario_for(symbol, key)
            if old is not None:
                child.scenarios[symbol] = old
            if execution is not None and execution.owner == key:
                continue
            observed = child.observe(symbol, context, candles, structure, enabled, now,
                # Children only replace top-level status/reason. Nested market
                # evidence is read-only; public() still detaches it for callers.
                assessment=(dict(assessment[0]), [c for c in assessment[1] if c["owner"] == key]))
        active = [s for child in self.children.values()
                  if (s := child.scenarios.get(symbol)) is not None and s.state not in TERMINAL]
        # Compatibility summary only. This representative grants no exclusivity.
        representative = execution or (previous if previous in active else next(iter(active), None))
        if representative is not None:
            self.scenarios[symbol] = representative
        elif previous is not None:
            self.scenarios[symbol] = previous
        return representative

    def context_for(self, context, symbol, strategy):
        s = self.scenario_for(symbol, strategy)
        # Scenario.public already detaches every mutable field from its owner.
        return replace(context, scenario=s.public() if s and s.state not in TERMINAL else None) if context else None

    def accept_decision(self, symbol, decision, now, book):
        child = self.children[decision.strategy]
        s = self.scenario_for(symbol, decision.strategy)
        if s is not None:
            child.scenarios[symbol] = s
        if decision.tradeable:
            prices=(decision.entry,decision.stop,decision.target)
            sign=1 if decision.action==Action.LONG else -1
            if (any(not isinstance(x,(int,float)) or not isfinite(x) or x<=0 for x in prices)
                    or sign*(decision.entry-decision.stop)<=0 or sign*(decision.target-decision.entry)<=0):
                self.reject(symbol,"geometry","incomplete_or_invalid_geometry",now,strategy=decision.strategy)
                return StrategyDecision(decision.strategy,Action.WAIT,["incomplete_or_invalid_geometry"],
                                        details={"state":"invalid_geometry"})
        result = child.accept_decision(symbol, decision, now, book)
        if result.tradeable and s and s.frozen and s.object_ref:
            # Source renaming and moving a target cannot rejuvenate the same
            # causal plan. Distinct object/side/episode/stop remains independent.
            key = (symbol, s.side, s.object_ref.token, s.episode_key,
                   s.frozen["entry"], s.frozen["stop"])
            original = self._plans.setdefault(key, (deepcopy(s.frozen), s.first_signal_mono, s.expires_mono))
            s.frozen, s.first_signal_mono, s.expires_mono = deepcopy(original[0]), original[1], original[2]
            for name in ("entry", "stop", "target", "watched_level"):
                setattr(result, name, s.frozen[name])
            result.details["scenario"] = s.public()
        return result

    def ready_key(self, symbol, strategy):
        s = self.scenario_for(symbol, strategy)
        return (s.first_signal_mono if s and s.first_signal_mono is not None else float("inf"),
                PRIORITY.index(strategy), s.scenario_id if s else "")

    def reject(self, symbol, owner, reason, now, *, strategy=None):
        s = self.scenario_for(symbol, strategy) if strategy else self.scenarios.get(symbol)
        if s:
            s.last_rejection = dict(owner=owner, reason=reason, atMono=now)

    def submitted(self, symbol, now, *, strategy=None):
        s = self.scenario_for(symbol, strategy) if strategy else self.scenarios[symbol]
        current = self.executions.get(symbol)
        if current is not None and current is not s:
            raise RuntimeError("symbol execution already reserved")
        if s is None or s.state in TERMINAL:
            raise RuntimeError("cannot reserve absent/terminal scenario")
        self.executions[symbol] = s
        self.scenarios[symbol] = s
        self.children[s.owner].scenarios[symbol] = s
        self.transition(s, "RESERVING", now, "atomic shared executor admission")
        super().submitted(symbol, now)

    def restore_execution(self, symbol, plan, now):
        saved = (getattr(plan, "strategy_details", None) or {}).get("scenario", {})
        own = self.scenario_for(symbol, plan.strategy)
        if own and (not saved or own.scenario_id == saved.get("scenarioId")):
            self.scenarios[symbol] = own
        restored = super().restore_execution(symbol, plan, now)
        self.executions[symbol] = restored
        self.children[restored.owner].scenarios[symbol] = restored
        return restored

    def cancelled(self, symbol, now, reason, **kwargs):
        super().cancelled(symbol, now, reason, **kwargs)
        current = self.executions.get(symbol)
        if current and current.state in TERMINAL:
            self.executions.pop(symbol, None)
            self.invalidate_preparations(symbol, now, "execution_released_requires_new_episode")

    def completed(self, symbol, now, reason):
        super().completed(symbol, now, reason)
        self.executions.pop(symbol, None)
        self.invalidate_preparations(symbol, now, "execution_released_requires_new_episode")

    def invalidate_preparations(self, symbol, now, reason):
        self.epochs[symbol] = self.epochs.get(symbol, 0) + 1
        for key, s in self.all_for(symbol).items():
            if s.state not in TERMINAL | BUSY:
                self.children[key].transition(s, "INVALIDATED", now, reason)

    def drain(self):
        events = super().drain()
        for child in self.children.values():
            events.extend(child.drain())
        return [(symbol, {**payload, "schemaVersion":3}) for symbol, payload in events]

    def public(self, symbol):
        result = super().public(symbol)
        scenarios = self.all_for(symbol)
        execution = self.executions.get(symbol)
        result.update(schemaVersion=3, scenarios={key:s.public() for key,s in scenarios.items()},
                      execution=execution.public() if execution else None,
                      busyReason=execution.state if execution else None,
                      activationEpoch=self.epochs.get(symbol, 0),
                      readyProposals=[s.public() for s in sorted(scenarios.values(),
                          key=lambda s:self.ready_key(symbol,s.owner)) if s.state == "ARMED"])
        return result
