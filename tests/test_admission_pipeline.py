from time import time

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scenario_support import assigned


def candidate(engine, symbol, target=102):
    session = ActiveSymbolSession(symbol, orderbook=OrderBook([(100, 10000)], [(100.01, 10000)]),
        last_price=100, last_book_at=time(), last_market_at=time(), book_synced=True)
    decision = StrategyDecision("level_breakout", Action.LONG, [], entry=100.01,
        stop=99.5, target=target, setup_id=symbol,
        details={"fireTrigger": {"observedAtMs": 123456, "price": 100.01}})
    session.decisions[decision.strategy] = decision
    engine.sessions[symbol] = session
    assigned(engine, session, decision.strategy)
    return session, decision


@pytest.mark.asyncio
@pytest.mark.parametrize("maker", [False, True])
async def test_one_pass_admits_multiple_symbols_with_current_budget(tmp_path, maker):
    config = Settings(_env_file=None, session_dir=str(tmp_path), exchange_clock_enabled=False)
    engine = TradingEngine(config)
    try:
        for symbol in ("AAA", "BBB"):
            candidate(engine, symbol)
        builds = []
        original = engine._build_risk_plan_for_opportunity
        def build(session, *args):
            builds.append((session.symbol, engine.broker.available_risk_usd))
            result = original(session, *args)
            if maker and result.plan:
                result.plan.entry_mode = "maker_limit"
            return result
        engine._build_risk_plan_for_opportunity = build
        engine.running = True
        engine._arbitrate_once()
        owners = set(engine.broker.positions) | set(engine.broker.pending_entries)
        assert owners == {"AAA", "BBB"}
        assert len(engine.router.executions) == 2
        assert min(b for _, b in builds) < builds[0][1]
        assert engine.broker.open_risk_usd <= engine.broker.balance * config.max_total_risk_fraction + 1e-6
        engine._arbitrate_once()
        assert len(engine.router.executions) == 2
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_economic_rejection_stays_prepared_and_success_emits_fire(tmp_path):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path), exchange_clock_enabled=False))
    try:
        _, rejected = candidate(engine, "BAD", 100.02)
        _, accepted = candidate(engine, "GOOD")
        engine.running = True
        engine._arbitrate_once()
        assert rejected.details["admission"]["stage"] == "PREPARED_INTENT"
        assert rejected.details["admission"]["rejectionReason"]
        assert accepted.details["admission"]["stage"] == "FIRE"
        fires = [e for e in engine.events if e["event"] == "admission_fire"]
        assert [e["symbol"] for e in fires] == ["GOOD"]
        assert fires[0]["payload"]["plan"]["net_at_target"] > 0
        assert rejected.details["fireTrigger"]["observedAtMs"] == 123456
        assert "BAD" not in engine.router.executions
    finally:
        await engine.close()
