from datetime import datetime, timezone

from scalp_bot.domain import Candle
from scalp_bot.strategy.level_engine import LevelEngine, measure_interactions
from scalp_bot.strategy.structure import StructuralLevel, build_market_structure


def c(i: int, o: float, h: float, l: float, close: float) -> Candle:
    return Candle(i * 60_000, o, h, l, close, 100, 10_000)


def test_level_engine_keeps_stable_id_across_rebuilds() -> None:
    rows = []
    peaks = {10: 100.00, 30: 100.03, 50: 99.99}
    for i in range(70):
        base = 98.5 + (i % 7) * 0.04
        high = peaks.get(i, base + 0.15)
        rows.append(c(i, base, high, base - 0.10, min(high - 0.02, base + 0.03)))

    engine = LevelEngine("TESTUSDT")
    first = engine.update(rows, [], 99.8)
    target1 = min(
        (x for x in first.levels if x.kind == "resistance"),
        key=lambda x: abs(x.center - 100),
    )
    second = engine.update(rows + [c(70, 99.4, 99.6, 99.3, 99.5)], [], 99.5)
    target2 = min(
        (x for x in second.levels if x.kind == "resistance"),
        key=lambda x: abs(x.center - 100),
    )
    assert target1.level_id == target2.level_id
    assert target1.generation == target2.generation


def test_distinct_approaches_are_not_same_as_pivot_count() -> None:
    level = StructuralLevel(
        kind="resistance",
        low=99.95,
        high=100.05,
        touches=5,
        timeframe="1m",
        score=0.8,
    )
    rows = [
        c(0, 99.0, 99.4, 98.9, 99.3),
        c(1, 99.7, 100.02, 99.6, 99.8),
        c(2, 99.8, 100.03, 99.7, 99.9),
        c(3, 99.2, 99.4, 99.0, 99.1),
        c(4, 99.7, 100.01, 99.6, 99.8),
        c(5, 99.1, 99.3, 98.9, 99.0),
    ]
    metrics = measure_interactions(rows, level)
    assert metrics["approaches"] == 2
    assert metrics["dwell"] >= 3


def test_structure_contains_previous_day_and_rolling_24h_extremes() -> None:
    day1 = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp() * 1000)
    context = []
    for i in range(96):
        context.append(
            Candle(
                day1 + i * 900_000,
                100,
                102,
                98,
                100,
                100,
                10_000,
            )
        )
    day2 = int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp() * 1000)
    for i in range(20):
        context.append(
            Candle(
                day2 + i * 900_000,
                101,
                103 + i * 0.01,
                99,
                101,
                100,
                10_000,
            )
        )
    rows = [c(i, 100, 100.2, 99.8, 100) for i in range(80)]
    structure = build_market_structure(rows, context, 101)
    assert structure.previous_day_high == 102
    assert structure.previous_day_low == 98
    assert structure.rolling_24h_high is not None
    assert structure.rolling_24h_low is not None
