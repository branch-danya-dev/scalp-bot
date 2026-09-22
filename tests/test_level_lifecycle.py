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



def test_initial_historical_counts_are_not_double_counted() -> None:
    tracker = LevelLifecycleTracker()
    level = StructuralLevel(
        kind="support",
        low=99.9,
        high=100.1,
        touches=3,
        timeframe="5m",
        score=0.8,
        distinct_approaches=3,
        dwell_bars=4,
        acceptance_bars=2,
    )
    rows = [candle(i, 100.0) for i in range(20)]
    structure = MarketStructure(levels=[level])

    tracker.update(structure, rows, 100.0, 10_000)

    assert level.distinct_approaches == 3
    assert level.dwell_bars == 4
    assert level.acceptance_bars == 2


def test_failed_break_and_sweep_count_once_per_bar() -> None:
    tracker = LevelLifecycleTracker()
    level = StructuralLevel(
        kind="resistance",
        low=100.0,
        high=100.1,
        touches=2,
        timeframe="5m",
        score=0.8,
    )
    rows = [candle(i, 99.0) for i in range(19)]
    rows.append(Candle(19 * 60_000, 99.9, 100.3, 99.8, 99.9, 100, 10_000))
    structure = MarketStructure(levels=[level])

    tracker.update(structure, rows, 99.9, 20_000)
    first_failed = level.failed_breaks
    first_sweeps = level.sweeps
    tracker.update(structure, rows, 99.9, 20_500)

    assert first_failed == 1
    assert first_sweeps == 1
    assert level.failed_breaks == 1
    assert level.sweeps == 1


def test_broken_level_gets_new_generation_after_disappearing() -> None:
    tracker = LevelLifecycleTracker()
    level = StructuralLevel(
        kind="resistance",
        low=100.0,
        high=100.1,
        touches=3,
        timeframe="5m",
        score=0.8,
    )
    rows = [candle(i, 99.0) for i in range(19)]
    rows.append(Candle(19 * 60_000, 100.0, 100.4, 99.9, 100.3, 100, 10_000))
    structure = MarketStructure(levels=[level])

    tracker.update(structure, rows, 100.3, 1_000)
    first_generation = level.generation_id
    assert level.lifecycle == "broken"

    # Same detector level reappears after being absent for more than a minute.
    replacement = StructuralLevel(
        kind="resistance",
        low=100.0,
        high=100.1,
        touches=2,
        timeframe="5m",
        score=0.8,
    )
    replacement_structure = MarketStructure(levels=[replacement])
    tracker.update(replacement_structure, rows[:-1], 99.0, 70_000)

    assert replacement.generation_id != first_generation
    assert replacement.lifecycle != "broken"



def test_level_id_is_stable_across_small_detector_drift() -> None:
    tracker = LevelLifecycleTracker()
    rows = [candle(i, 99.0) for i in range(20)]

    first = StructuralLevel(
        kind="resistance",
        low=100.00,
        high=100.10,
        touches=3,
        timeframe="5m",
        score=0.8,
    )
    tracker.update(MarketStructure(levels=[first]), rows, 99.5, 1_000)

    shifted = StructuralLevel(
        kind="resistance",
        low=100.01,
        high=100.11,
        touches=3,
        timeframe="5m",
        score=0.8,
    )
    tracker.update(MarketStructure(levels=[shifted]), rows, 99.5, 2_000)

    assert shifted.level_id == first.level_id
    assert shifted.generation_id == first.generation_id


def test_lifecycle_timestamps_are_exposed_on_level() -> None:
    tracker = LevelLifecycleTracker()
    rows = [candle(i, 99.0) for i in range(20)]
    level = StructuralLevel(
        kind="support",
        low=99.8,
        high=99.9,
        touches=2,
        timeframe="5m",
        score=0.7,
    )
    structure = MarketStructure(levels=[level])

    tracker.update(structure, rows, 99.0, 10_000)
    tracker.update(structure, rows, 99.85, 20_000)

    assert level.first_seen_ms == 10_000
    assert level.last_seen_ms == 20_000
    assert level.last_approach_ms == 20_000
