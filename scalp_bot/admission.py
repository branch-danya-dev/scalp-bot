"""Prepared strategy geometry -> economic plan -> atomic portfolio admission.

StrategyDecision.tradeable remains a legacy geometry predicate. Only admission
FIRE is executable. A preview never grants authority or reserves capital.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .domain import StrategyDecision, TradePlan
from .input_scope import input_scope
from .execution_book import coherent_execution_book
from .latency_observability import latency_snapshot, observe_latency, span, stream_name
from .strategy import SelectionPriority, SemanticCandidateAssessment, build_selection_priority
from .strategy_policy import minimum_expectancy_r
from .setup_segments import setup_segment

if TYPE_CHECKING:
    from .engine import ActiveSymbolSession


@dataclass(frozen=True, slots=True)
class PreparedIntent:
    symbol: str
    strategy: str
    side: str
    setup_id: str
    scenario_id: str | None
    episode_key: str | None
    first_ready_mono: float
    entry: float
    stop: float
    target: float


@dataclass(frozen=True, slots=True)
class EconomicPlan:
    intent: PreparedIntent
    plan: TradePlan
    checked_at_ns: int
    available_notional: float
    available_risk_usd: float


@dataclass(slots=True)
class Opportunity:
    priority: SelectionPriority
    arbitration: SemanticCandidateAssessment
    session: ActiveSymbolSession
    decision: StrategyDecision
    plan: TradePlan
    position_action: str = "open"
    economic_plan: EconomicPlan | None = None


class AdmissionEngine:
    def _segment_admission(self, session, decision, plan):
        segment = setup_segment(decision.strategy, decision.action.value, plan.strategy_details,
            entry=plan.market_entry, stop=plan.stop, context=session.market_context)
        evidence = self.segment_expectancy.assess(segment,
            mode=self.config.segment_expectancy_mode,
            min_samples=self.config.segment_expectancy_min_samples,
            minimum_expectancy_r=minimum_expectancy_r(self.config, decision.strategy))
        plan.strategy_details["setupSegment"] = segment
        plan.strategy_details["segmentExpectancy"] = evidence
        decision.details["segmentExpectancy"] = evidence
        if evidence["blocked"]:
            self._risk_reject_if_changed(session, decision, "segment expectancy gate",
                diagnostics={"segmentExpectancy": evidence, "rejectionOwner": "expectancy"})
            return False
        return True

    def _prepare_intent(self, session, decision):
        scenario = self.router.scenario_for(session.symbol, decision.strategy)
        prior = decision.details.get("admission", {})
        intent = PreparedIntent(session.symbol, decision.strategy, decision.action.value,
            decision.setup_id or self._resolve_setup_id(session, decision),
            scenario.scenario_id if scenario else None, scenario.episode_key if scenario else None,
            scenario.first_signal_mono if scenario and scenario.first_signal_mono is not None
            else prior.get("firstReadyMono", self.clock.perf_counter_ns()/1e9),
            float(decision.entry), float(decision.stop), float(decision.target))
        decision.details["admission"] = dict(stage="PREPARED_INTENT", contractVersion=1,
            firstReadyMono=intent.first_ready_mono, intent=asdict(intent))
        return intent

    def _economic_plan(self, opportunity):
        intent = PreparedIntent(**opportunity.decision.details["admission"]["intent"])
        opportunity.economic_plan = EconomicPlan(intent, opportunity.plan,
            self.clock.perf_counter_ns(), self.broker.available_notional, self.broker.available_risk_usd)
        opportunity.decision.details["admission"].update(stage="ECONOMIC_PLAN",
            checkedAtNs=opportunity.economic_plan.checked_at_ns)
        opportunity.plan.strategy_details["semanticArbitration"] = opportunity.arbitration.public()
        opportunity.plan.strategy_details["selectionPriority"] = opportunity.priority.public()
        opportunity.plan.strategy_details["strategyExpectancy"] = self.expectancy.snapshot(
            opportunity.decision.strategy, min_samples=self.config.strategy_expectancy_min_samples,
            minimum_expectancy_r=minimum_expectancy_r(self.config, opportunity.decision.strategy))

    def _mark_admission_fire(self, opportunity):
        if opportunity.economic_plan is None:
            raise RuntimeError("FIRE requires a current economic plan")
        now = self.clock.perf_counter_ns()
        opportunity.decision.details["admission"].update(stage="FIRE", fireMonoNs=now)
        opportunity.plan.strategy_details["admission"] = dict(opportunity.decision.details["admission"])
        message = self._latency_for_selected_opportunity(opportunity)
        if message is not None:
            message.fire_mono_ns = now
            observe_latency("strategy_to_fire", max(0, now-message.strategy_eval_started_mono_ns)/1e9,
                stream=stream_name(message.topic), strategy=opportunity.decision.strategy)
        self._emit("admission_fire", opportunity.session.symbol,
            {"admission": opportunity.plan.strategy_details["admission"], "plan": opportunity.plan.public()})

    @input_scope("arbiter", symbol_arg=False)
    def _arbitrate_once(self) -> None:
        self._record_input("callback", None, {"name": "arbiter"})
        opportunities: list[Opportunity] = []
        now = self.clock.time()
        clock = self._clock_state()
        if clock is not None and not clock["valid"]:
            self._cancel_all_pending("clock_invalid")
            for session in self.sessions.values():
                session.decisions.clear()
            return
        for event in self.broker.expire_pending(now):
            self._pop_pending_order_latency(
                str(event.get("symbol") or ""),
                str(event.get("setupId") or ""),
            )
            self._emit(
                "entry_cancelled",
                event.get("symbol"),
                event,
            )
        candidate_map = {
            item.symbol: item
            for item in self.candidates
        }

        for session in self.sessions.values():
            if self._clock_entry_block(session):
                continue
            if session.symbol in self.broker.pending_entries:
                continue
            existing_position = self.broker.positions.get(
                session.symbol
            )
            if (
                session.last_market_at <= 0
                or now - session.last_market_at
                > self.config.market_stale_seconds
            ):
                continue
            if not session.book_is_fresh(now):
                continue
            if not session.deep_book_is_fresh(now):
                continue
            if not session.confirmed_candle_is_fresh(
                self.config.confirmed_candle_stale_seconds,
                clock["evaluationMs"] / 1000 if clock is not None else now,
            ):
                continue

            planned: list[tuple[
                StrategyDecision,
                object,
                SemanticCandidateAssessment,
                int,
                float,
                str,
            ]] = []

            for decision in session.decisions.values():
                if decision.strategy == "orderbook_density":
                    continue
                if (
                    not decision.tradeable
                    or not self.strategy_enabled.get(
                        decision.strategy,
                        False,
                    )
                ):
                    continue

                self._prepare_intent(session, decision)
                setup_id = self._resolve_setup_id(
                    session,
                    decision,
                )
                decision.setup_id = setup_id
                staged_entry = (
                    decision.details.get("stagedEntry")
                    if isinstance(
                        decision.details.get("stagedEntry"),
                        dict,
                    )
                    else {}
                )
                staged_phase = str(
                    staged_entry.get("phase") or "full"
                )
                position_action = (
                    "add"
                    if existing_position is not None
                    else "open"
                )
                if existing_position is not None:
                    if staged_phase != "add":
                        continue
                    if (
                        existing_position.strategy
                        != decision.strategy
                        or existing_position.side
                        != decision.side
                        or existing_position.setup_id
                        != setup_id
                    ):
                        continue
                elif staged_phase == "add":
                    # The probe may have been rejected or timed out. Never
                    # execute an orphaned add as if a position existed.
                    continue

                if not self._scenario_entry_valid(session, decision):
                    continue
                base_assessment = self._owned_assessment(session, decision)
                decision.details["semanticArbitration"] = (
                    base_assessment.public()
                )
                decision.details["riskScale"] = (
                    base_assessment.risk_scale
                )
                decision.details["riskScaleSource"] = (
                    "scenario_size_policy"
                )
                if not base_assessment.allowed:
                    self._record_arbiter_blocked(
                        session,
                        decision,
                        base_assessment,
                    )
                    continue

                policy_pre = self._evaluate_research_policy(
                    session,
                    decision,
                    phase="pre_plan",
                    arbitration=base_assessment,
                )
                if policy_pre.blocked:
                    continue

                blocked_reason = self._setup_blocked_reason(
                    session,
                    decision.strategy,
                    setup_id,
                    now,
                )
                if blocked_reason:
                    self._record_setup_blocked(
                        session,
                        decision,
                        blocked_reason,
                    )
                    continue

                expectancy_snapshot = self.expectancy.snapshot(
                    decision.strategy,
                    min_samples=(
                        self.config.strategy_expectancy_min_samples
                    ),
                    minimum_expectancy_r=minimum_expectancy_r(
                        self.config,
                        decision.strategy,
                    ),
                )
                if (
                    self.config.enforce_strategy_expectancy_gate
                    and expectancy_snapshot["sampleReady"]
                    and expectancy_snapshot["status"] == "negative"
                ):
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        (
                            "strategy expectancy gate: "
                            f"{expectancy_snapshot['expectancyR']:.3f}R < "
                            f"{expectancy_snapshot['minimumExpectancyR']:.3f}R"
                        ),
                        diagnostics={
                            "expectancy": expectancy_snapshot,
                            "semanticArbitration": (
                                base_assessment.public()
                            ),
                        },
                    )
                    continue

                if position_action == "add":
                    allowed = (
                        existing_position is not None
                        and not existing_position.partial_taken
                        and self.broker.available_notional > 0
                        and self.broker.available_risk_usd > 0
                    )
                    portfolio_reason = (
                        "allowed"
                        if allowed
                        else "staged add portfolio budget exhausted"
                    )
                else:
                    allowed, portfolio_reason = (
                        self.broker.can_open(session.symbol)
                    )
                if not allowed:
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        portfolio_reason,
                        diagnostics={
                            "semanticArbitration": (
                                base_assessment.public()
                            ),
                        },
                    )
                    continue

                result = self._build_risk_plan_for_opportunity(
                    session,
                    decision,
                    setup_id,
                    position_action,
                    existing_position,
                )
                if not result.allowed or result.plan is None:
                    diagnostics = dict(
                        result.diagnostics or {}
                    )
                    diagnostics["semanticArbitration"] = (
                        base_assessment.public()
                    )
                    self._risk_reject_if_changed(
                        session,
                        decision,
                        result.reason,
                        diagnostics=diagnostics,
                    )
                    continue

                # The owner supplied one frozen hypothesis. Economics does not
                # send it back through another context-selection pass.
                result.plan.strategy_details["semanticArbitration"] = base_assessment.public()
                result.plan.strategy_details["strategyExpectancy"] = expectancy_snapshot
                if not self._segment_admission(session, decision, result.plan):
                    continue

                if (
                    position_action == "open"
                    and result.plan.entry_mode == "maker_limit"
                ):
                    allowed, pending_reason = (
                        self.broker.can_place_pending(
                            session.symbol
                        )
                    )
                    if not allowed:
                        self._risk_reject_if_changed(
                            session,
                            decision,
                            pending_reason,
                            diagnostics={
                                "semanticArbitration": (
                                    base_assessment.public()
                                ),
                            },
                        )
                        continue

                economics = (
                    result.plan.strategy_details.get(
                        "economics"
                    )
                    if isinstance(
                        result.plan.strategy_details,
                        dict,
                    )
                    else None
                )
                shadow_reasons = (
                    tuple(
                        economics.get(
                            "shadowRejectReasons"
                        )
                        or []
                    )
                    if isinstance(economics, dict)
                    else ()
                )
                shadow_fingerprint = (
                    decision.strategy,
                    setup_id,
                    shadow_reasons,
                )
                if shadow_reasons:
                    if (
                        session.last_economic_shadow_fingerprint
                        != shadow_fingerprint
                    ):
                        session.last_economic_shadow_fingerprint = (
                            shadow_fingerprint
                        )
                        self._emit(
                            "economic_shadow",
                            session.symbol,
                            {
                                "strategy": decision.strategy,
                                "setupId": setup_id,
                                "shadowRejectReasons": list(
                                    shadow_reasons
                                ),
                                "economics": economics,
                                "decision": decision.public(),
                                "semanticArbitration": (
                                    base_assessment.public()
                                ),
                            },
                            snapshot=True,
                        )
                else:
                    session.last_economic_shadow_fingerprint = (
                        None
                    )

                policy_post = self._evaluate_research_policy(
                    session,
                    decision,
                    phase="post_plan",
                    plan=result.plan,
                    arbitration=base_assessment,
                )
                if policy_post.blocked:
                    continue

                scanner_candidate = candidate_map.get(
                    session.symbol
                )
                activity_rank = (
                    scanner_candidate.activity_rank
                    if (
                        scanner_candidate
                        and scanner_candidate.activity_rank
                    )
                    else 99
                )
                activity_score = (
                    scanner_candidate.activity_score
                    if scanner_candidate
                    else 0.0
                )
                planned.append(
                    (
                        decision,
                        result.plan,
                        base_assessment,
                        int(activity_rank),
                        float(activity_score),
                        position_action,
                    )
                )

            if not planned:
                continue

            final_assessments = {row[0].strategy: row[2] for row in planned}
            for (
                decision,
                plan,
                base_assessment,
                activity_rank,
                activity_score,
                position_action,
            ) in planned:
                assessment = final_assessments.get(
                    decision.strategy,
                    base_assessment,
                )
                decision.details["semanticArbitration"] = (
                    assessment.public()
                )
                if not assessment.allowed:
                    self._record_arbiter_blocked(
                        session,
                        decision,
                        assessment,
                    )
                    continue

                policy_final = self._evaluate_research_policy(
                    session,
                    decision,
                    phase="final",
                    plan=plan,
                    arbitration=assessment,
                )
                if policy_final.blocked:
                    continue

                session.arbiter_block_fingerprints.pop(
                    decision.strategy,
                    None,
                )
                plan.strategy_details[
                    "semanticArbitration"
                ] = assessment.public()
                priority = build_selection_priority(
                    assessment,
                    plan,
                    activity_rank=activity_rank,
                    activity_score=activity_score,
                )
                plan.strategy_details[
                    "selectionPriority"
                ] = priority.public()
                opportunities.append(
                    Opportunity(
                        priority=priority,
                        arbitration=assessment,
                        session=session,
                        decision=decision,
                        plan=plan,
                        position_action=position_action,
                    )
                )

        if not opportunities:
            return

        winners = {}
        def ready_priority(item):
            ready = self.router.ready_key(item.session.symbol, item.decision.strategy)
            if not self.config.trading_quality_enabled:
                return ready
            details = item.plan.strategy_details
            return (-details.get("alignmentPriority", 0), -item.plan.net_reward_risk,
                    -details.get("crossVenuePriority", 0), -float(details.get("setupQuality", 0)), ready)
        for item in sorted(opportunities, key=ready_priority):
            symbol = item.session.symbol
            if symbol not in winners:
                winners[symbol] = item
            else:
                self.router.reject(symbol, "dispatcher", "trading_quality_priority" if self.config.trading_quality_enabled else "earlier_eligible_ready_proposal",
                    self.clock.perf_counter_ns()/1e9, strategy=item.decision.strategy)
        ordered = sorted(winners.values(), key=lambda item: item.priority.key(), reverse=True)
        if self.prepared_ranker is not None:
            ordered = self.prepared_ranker.rank(ordered, self.clock.perf_counter_ns())
        for best in ordered:
            # The preceding submission has already reserved budget, including
            # pending makers and paired execution. Never execute a preview size.
            if not self._scenario_entry_valid(best.session, best.decision):
                continue
            result = self._build_risk_plan_for_opportunity(best.session, best.decision,
                best.decision.setup_id, best.position_action,
                self.broker.positions.get(best.session.symbol))
            if not result.allowed or result.plan is None:
                self._risk_reject_if_changed(best.session, best.decision, result.reason,
                                            diagnostics=result.diagnostics)
                continue
            best.plan = result.plan
            if not self._segment_admission(best.session, best.decision, best.plan):
                continue
            for phase in ("post_plan", "final"):
                if self._evaluate_research_policy(best.session, best.decision, phase=phase,
                        plan=best.plan, arbitration=best.arbitration).blocked:
                    break
            else:
                self._economic_plan(best)
                self._dispatch_admitted(best, now)

    def _dispatch_admitted(self, best, now):
        if self._clock_entry_block(best.session):
            return
        if best.position_action == "add":
            allowed, reason = self.broker.can_add(
                best.plan
            )
        elif best.plan.entry_mode == "maker_limit":
            allowed, reason = self.broker.can_place_pending(
                best.session.symbol
            )
        else:
            allowed, reason = self.broker.can_open(
                best.session.symbol
            )
        if not allowed:
            self._risk_reject_if_changed(
                best.session,
                best.decision,
                reason,
                diagnostics={
                    "semanticArbitration": (
                        best.arbitration.public()
                    ),
                    "selectionPriority": (
                        best.priority.public()
                    ),
                },
            )
            return

        if self.prepared_ranker is not None:
            intent = PreparedIntent(**best.decision.details["admission"]["intent"])
            if not self.prepared_ranker.assess(intent, self.clock.perf_counter_ns())[0]:
                return
        if self._submit_research_opportunity(best):
            return

        self._mark_admission_fire(best)
        best.session.last_risk_fingerprint = None
        best.session.last_blocked_fingerprint = None
        latency_message = (
            self._latency_for_selected_opportunity(best)
        )
        selection_payload = {
            "latencyTrace": latency_snapshot(
                latency_message
            ),
            "semanticArbitration": (
                best.arbitration.public()
            ),
            "selectionPriority": best.priority.public(),
            "playbookSetupQuality": float(
                best.decision.details.get(
                    "setupQuality",
                    best.decision.confidence,
                )
                or 0.0
            ),
        }

        self.router.submitted(best.session.symbol, self.clock.perf_counter_ns()/1e9, strategy=best.decision.strategy)
        best.plan.strategy_details["scenario"] = self.router.scenarios[best.session.symbol].public()
        self._scenario_events()
        if best.plan.entry_mode == "maker_limit":
            execution_mode = "paper_maker"
            self._mark_order_sent(
                latency_message,
                strategy=best.decision.strategy,
                execution_mode=execution_mode,
            )
            with span(
                "paper.order.submit",
                **{
                    "market.symbol": best.session.symbol,
                    "strategy.name": best.decision.strategy,
                    "execution.mode": execution_mode,
                    "market.event_id": (
                        latency_message.event_id
                        if latency_message is not None
                        else None
                    ),
                },
            ):
                if best.position_action == "add":
                    pending = self.broker.place_pending_add(
                        best.plan,
                        min_trade_ts_ms=(
                            best.session.trades[-1].ts_ms
                            if best.session.trades
                            else None
                        ),
                    )
                    pending_event = "entry_add_pending"
                else:
                    pending = self.broker.place_pending(
                        best.plan,
                        min_trade_ts_ms=(
                            best.session.trades[-1].ts_ms
                            if best.session.trades
                            else None
                        ),
                    )
                    pending_event = "entry_pending"
            self._mark_order_ack(
                latency_message,
                strategy=best.decision.strategy,
                execution_mode=execution_mode,
            )
            if latency_message is not None:
                self._pending_order_latency[
                    (
                        best.session.symbol,
                        str(best.plan.setup_id),
                    )
                ] = latency_message
                best.plan.strategy_details[
                    "latencyTrace"
                ] = latency_snapshot(latency_message)
                selection_payload["latencyTrace"] = (
                    latency_snapshot(latency_message)
                )
            self._emit(
                pending_event,
                best.session.symbol,
                {
                    "plan": best.plan.public(),
                    "pending": pending.public(),
                    "reasons": best.decision.reasons,
                    "visuals": best.decision.visuals,
                    **selection_payload,
                },
                snapshot=True,
            )
            return

        execution_mode = "paper_taker"
        self._mark_order_sent(
            latency_message,
            strategy=best.decision.strategy,
            execution_mode=execution_mode,
        )
        best.session.last_trade_at = now
        with span(
            "paper.order.submit",
            **{
                "market.symbol": best.session.symbol,
                "strategy.name": best.decision.strategy,
                "execution.mode": execution_mode,
                "market.event_id": (
                    latency_message.event_id
                    if latency_message is not None
                    else None
                ),
            },
        ):
            if best.position_action == "add":
                position = self.broker.add(
                    best.plan,
                    coherent_execution_book(best.session.orderbook, best.session.depth_orderbook()),
                )
            else:
                position = self.broker.open(
                    best.plan,
                    coherent_execution_book(best.session.orderbook, best.session.depth_orderbook()),
                )

        self._mark_order_ack(
            latency_message,
            strategy=best.decision.strategy,
            execution_mode=execution_mode,
        )
        self._mark_order_fill(
            latency_message,
            strategy=best.decision.strategy,
            execution_mode=execution_mode,
        )
        if latency_message is not None:
            best.plan.strategy_details[
                "latencyTrace"
            ] = latency_snapshot(latency_message)

        if best.position_action == "add":
            self._record_added_position(
                best.session,
                best.decision,
                best.plan.public(),
                position.public(),
                semantic_arbitration=(
                    best.arbitration.public()
                ),
                selection_priority=best.priority.public(),
            )
        else:
            self._record_opened_position(
                best.session,
                best.decision,
                best.plan.public(),
                position.public(),
                semantic_arbitration=(
                    best.arbitration.public()
                ),
                selection_priority=best.priority.public(),
            )

