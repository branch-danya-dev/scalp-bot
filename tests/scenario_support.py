"""Explicit owner fixtures for tests isolating execution from situation selection.

Production routing is tested separately; this helper never patches engine code.
"""
from scalp_bot.scenario import Scenario


def assigned(engine, session, owner, side="long"):
    now = engine.clock.perf_counter_ns()/1e9
    price = session.orderbook.mid or session.last_price or 100
    s = Scenario(session.symbol, f"{session.symbol}:unit-owner", owner, side,
                 "unit-market-basis", price, 1, now, now, now+300,
                 ["controlled assignment for downstream contract test"])
    engine.router.scenarios[session.symbol] = s
    return s
