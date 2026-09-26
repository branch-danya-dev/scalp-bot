"""Small engine integration for scenario ownership and preparation."""
from dataclasses import replace

from .domain import Action, StrategyDecision
from .scenario import TERMINAL
from .strategy.scenario_objects import decision_object_id
from .strategy.semantic_arbiter import SemanticCandidateAssessment, assess_structural_path


class ScenarioRuntime:
    def _scenario_events(self):
        for symbol, payload in self.router.drain():
            self._emit("scenario_transition", symbol, payload)

    def _route_scenario(self, session, candles):
        previous = self.router.scenarios.get(session.symbol)
        s = self.router.observe(session.symbol, session.market_context, candles, session.structure,
            self.strategy_enabled, self.clock.perf_counter_ns()/1e9,
            position=self.broker.positions.get(session.symbol),
            pending=self.broker.pending_entries.get(session.symbol))
        if s is not None and s is not previous:
            self.strategies[s.owner].reset(session.symbol)
        if session.market_context is not None:
            session.market_context = replace(session.market_context, scenario=s.public() if s else None)
        session.scenario_view = self.router.public(session.symbol)
        self._scenario_events()
        return s

    def _scenario_decision(self, session, decision):
        if decision.tradeable:
            decision.setup_id = self._resolve_setup_id(session, decision)
        scenario = self.router.scenarios.get(session.symbol)
        if scenario and scenario.state not in TERMINAL | {"ORDER_PENDING", "IN_POSITION"} and decision.details.get("acceptedBreak"):
            self.router.transition(scenario, "INVALIDATED", self.clock.perf_counter_ns()/1e9,
                                   "owner observed a confirmed structural break")
        decision = self.router.accept_decision(session.symbol, decision,
            self.clock.perf_counter_ns()/1e9, session.orderbook)
        session.scenario_view = self.router.public(session.symbol)
        self._scenario_events()
        return decision

    def _scenario_prepare(self, session, decision):
        s = self.router.scenarios.get(session.symbol)
        if s is None or s.state in TERMINAL or s.state in {"ORDER_PENDING","IN_POSITION"}:
            return
        # Preview uses existing RiskEngine and current unreserved budget. It is
        # informative only; the arbiter rebuilds atomically before submission.
        if decision.tradeable:
            if s.preview is not None and s.preview.get("stage") == "owner_plan":
                return
            candidate = decision
        else:
            # The owner supplies its geometry, including a structural target.
            # No capital is reserved and no confirmation is added here.
            strategy = self.strategies[s.owner]
            prepare = getattr(strategy, "prepare_plan", None)
            candidate = prepare(s, decision, session.orderbook,
                [c for c in session.candles if c.confirmed], session.structure) if prepare else None
            if candidate is None:
                return
            signature = (candidate.stop, candidate.target)
            if s.preview and s.preview.get("geometry") == signature:
                return
        result = self._build_risk_plan_for_opportunity(session,candidate,
            candidate.setup_id or s.scenario_id,"open",None)
        s.preview = dict(reservesCapital=False, allowed=result.allowed, reason=result.reason,
            plan=result.plan.public() if result.plan else None,
            geometry=(candidate.stop, candidate.target),
            stage="owner_plan" if decision.tradeable else "owner_preparation")
        session.scenario_view = self.router.public(session.symbol)

    def _owned_assessment(self, session, decision):
        """Compatibility telemetry; no second context/strategy contest."""
        s=self.router.scenarios.get(session.symbol)
        owned=bool(s and s.owner==decision.strategy and s.state not in TERMINAL
                   and s.side==decision.action.value
                   and (s.object_id is None or decision_object_id(decision)==s.object_id))
        path=assess_structural_path(decision,session.market_context,
            partial_take_at_r=self.config.partial_take_at_r,
            partial_take_enabled=self.config.partial_take_enabled)
        freshness=decision.details.get("opportunityFreshness",{}).get("classification","unknown")
        # Preserve conservative size reductions once, without converting them
        # into a context veto or treating a small position as a quality signal.
        scale=min(float(decision.details.get("riskScale", 1.0)), path.risk_scale, {"acceptable":.85,"unknown":.8,"late":.65}.get(freshness,1.0))
        if s and s.frozen:
            scale=min(scale,s.frozen.setdefault("riskScale",scale))
            s.frozen["riskScale"]=scale
        flow=decision.details.get("flowAlignment",{}).get("classification","unknown")
        return SemanticCandidateAssessment(decision.strategy,decision.setup_id,decision.action.value,
            owned,() if owned else ("scenario_not_owned",),(),(),path,str(flow),"context_only",
            freshness,0,0,0,scale,("strategy assigned before signal; no competing context arbitration",))

    def _scenario_entry_valid(self, session, decision):
        s=self.router.scenarios.get(session.symbol)
        if (not s or s.owner!=decision.strategy or s.state in TERMINAL
                or s.side!=decision.action.value
                or (s.object_id is not None and decision_object_id(decision)!=s.object_id)):
            return False
        now=self.clock.perf_counter_ns()/1e9
        if s.state != "IN_POSITION" and now>=s.expires_mono:
            self.router.transition(s,"EXPIRED",now,"final freshness check")
            self._scenario_events()
            return False
        freshness=decision.details.get("opportunityFreshness",{}).get("classification")
        if s.frozen and s.state != "IN_POSITION":
            quote = session.orderbook.executable_entry(decision.side)
            low, high = s.frozen["entryArea"]
            if quote is None or not low <= quote <= high:
                self.router.transition(s,"INVALIDATED",now,"final quote left frozen entry area")
                self._scenario_events()
                return False
        if freshness == "exhausted":
            self.router.transition(s,"EXPIRED",now,"frozen movement budget exhausted")
            self._scenario_events()
            return False
        reason=self.strategies[decision.strategy].entry_invalidation(
            decision,session.orderbook)
        if reason:
            self.router.reject(session.symbol,"strategy",reason,now)
            if s.state != "IN_POSITION":
                self.router.transition(s,"INVALIDATED",now,reason)
                self._scenario_events()
            self._risk_reject_if_changed(session,decision,reason,
                diagnostics={"rejectionOwner":"strategy","scenario":s.public()})
            return False
        return True
