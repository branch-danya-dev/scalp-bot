"""Unreserved owner geometry while waiting for its entry event.

This is indicative: no strategy event is manufactured and final risk always
rechecks the owner's completed plan. The engine never chooses a stop/target.
"""
from ..domain import Action, StrategyDecision
from .common import nearby_round_level, typical_range_abs
from .liquidity import find_liquidity_targets
from .targets import structural_target, movement_budget


def preparation_plan(strategy, scenario, decision, book, candles, structure):
    action = Action(scenario.side)
    long = action == Action.LONG
    price = book.executable_entry(action)
    if not price:
        return None
    span = typical_range_abs(candles)
    zone = scenario.level or decision.details.get("zone") or {}
    stop = None
    if strategy.key in {"level_breakout", "weak_level_rejection"} and zone.get("low") and zone.get("high"):
        low, high = zone["low"], zone["high"]
        if strategy.key == "level_breakout":
            buffer = max((high-low)*.45, span*.20, price*max(book.spread_pct*2, .00025))
            stop = low-buffer if long else high+buffer
        else:
            rounding = nearby_round_level((low+high)/2, max(span*.35, high-low))
            buffer = max(span*.20, price*.00015)
            stop = min(low, rounding or low)-buffer if long else max(high, rounding or high)+buffer
    elif strategy.key == "trend_structure":
        state = strategy._states.get(scenario.symbol)
        if state and state.test_line_price and state.test_extreme:
            buffer = max(price*.0008, abs(price-state.test_line_price)*.10)
            stop = min(state.test_extreme, state.test_line_price)-buffer if long else max(state.test_extreme, state.test_line_price)+buffer
    elif strategy.key == "price_action_hypothesis":
        hypothesis = decision.details.get("hypothesis", {})
        stop = hypothesis.get("invalidation")
    if stop is None:
        return None
    target, _, source = structural_target(price, action,
        find_liquidity_targets(candles, price, action, min_distance_pct=0, structure=structure),
        movement=movement_budget(candles))
    return StrategyDecision(strategy.key, action, ["owner preparation; entry event still required"],
        entry=price, stop=stop, target=target, setup_id=scenario.scenario_id,
        details={"expectedImpulsePct":movement_budget(candles)/price, "targetSource":source,
                 "riskScale":.65, "preparationOnly":True,
                 "scenario": {"scenarioId": scenario.scenario_id, "owner": scenario.owner,
                              "marketObjectId": scenario.object_id}})
