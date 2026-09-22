from scalp_bot.domain import Candle
from scalp_bot.strategy.lifecycle import LevelLifecycleTracker
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel


def candle(i: int, close: float) -> Candle:
    return Candle(i * 60_000, close, close + 0.1, close - 0.1, close, 100, 10_000)


def test_level_lifecycle_counts_separate_approaches() -> None:
    tracker = LevelLifecycleTracker()
    level = StructuralLevel(
        kind="resistance", low=100.0, high=100.1, touches=2,
        timeframe="5m", score=0.8, distinct_approaches=1,
    )
    structure = MarketStructure(levels=[level])
    rows = [candle(i, 99.0) for i in range(20)]
    tracker.update(structure, rows, 99.0, 1_000)
    tracker.update(structure, rows, 100.05, 2_000)
    tracker.update(structure, rows, 99.0, 3_000)
    tracker.update(structure, rows, 100.04, 4_000)
    assert level.level_id is not None
    assert level.generation_id is not None
    assert level.distinct_approaches >= 2


def test_structural_level_as_zone_preserves_last_touch_index() -> None:
    level = StructuralLevel(
        kind="support", low=99.9, high=100.0, touches=3,
        timeframe="5m", score=0.8, last_touch_index=17,
    )
    assert level.as_zone().last_touch_index == 17
