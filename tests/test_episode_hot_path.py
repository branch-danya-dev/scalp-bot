from types import SimpleNamespace
import random
from scalp_bot.scenario_episodes import breach_witness
from scalp_bot.scenario_identity import level_ref
from scalp_bot.strategy.structure import StructuralLevel


def reference(level,side,candles,forming,now):
    rows=[c for c in candles if c.confirmed and c.start_ms+60000<=now][-1:]
    if forming is not None and forming.start_ms<=now:rows.append(forming)
    witnesses=[c.start_ms for c in rows if (c.low<level.low if side=='long' else c.high>level.high)]
    return max(witnesses) if witnesses else None


def test_witness_matches_reference_across_rollover_corrections_and_future_bars():
    rng=random.Random(1729);level=SimpleNamespace(low=99,high=101)
    for _ in range(1000):
        candles=[SimpleNamespace(confirmed=rng.choice([True,False]),start_ms=rng.randrange(30)*60000,
                    low=rng.randrange(97,102),high=rng.randrange(101,105)) for n in range(rng.randrange(80))]
        forming=rng.choice(candles) if candles else None
        now=rng.randrange(30)*60000+rng.randrange(60000)
        for side in ('long','short'):
            assert breach_witness(level,side,candles,forming,now)==reference(level,side,candles,forming,now)


def test_identity_avoids_full_level_serialization_and_preserves_missing_generation():
    class Level:
        generation_id='same-generation'
        def public(self):raise AssertionError('unnecessary serialization')
    assert level_ref(Level())==level_ref({'generation_id':'same-generation'})
    level=Level();level.generation_id=None
    assert level_ref(level) is None
