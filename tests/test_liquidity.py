from scalp_bot.domain import Action, Candle
from scalp_bot.strategy.liquidity import find_liquidity_target
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel


def candle(i: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(i * 60_000, o, h, l, c, 100, 10_000)


def test_isolated_external_high_can_be_liquidity_target() -> None:
    rows = []
    for i in range(80):
        base = 100 + (i % 6) * 0.03
        high = base + 0.10
        if i == 20:
            high = 103.0
        rows.append(candle(i, base, high, base - 0.10, base + 0.02))

    target = find_liquidity_target(rows, 101.0, Action.LONG)
    assert target is not None
    assert target.kind in {"swing_high", "resistance_zone"}
    assert target.price > 101.0


def test_repeated_zone_is_valid_liquidity_target() -> None:
    rows = []
    touches = {15, 30, 45, 60}
    for i in range(80):
        base = 99.0 + (i % 5) * 0.02
        high = 102.0 if i in touches else base + 0.12
        rows.append(
            candle(i, base, high, base - 0.10, min(base + 0.03, high - 0.02))
        )

    target = find_liquidity_target(rows, 100.5, Action.LONG)
    assert target is not None
    assert target.price > 100.5



def quiet_rows(price: float = 100.0) -> list[Candle]:
    return [
        Candle(
            i * 60_000,
            price,
            price + 0.01,
            price - 0.01,
            price,
            1,
            price,
        )
        for i in range(30)
    ]


def test_day_and_previous_day_extremes_are_long_liquidity_targets() -> None:
    structure = MarketStructure(
        levels=[
            StructuralLevel(
                kind="previous_day_high",
                low=101.5,
                high=101.5,
                touches=1,
                timeframe="1D",
                score=0.88,
            ),
            StructuralLevel(
                kind="day_high",
                low=102.0,
                high=102.0,
                touches=1,
                timeframe="1D",
                score=0.92,
            ),
        ],
        day_high=102.0,
        previous_day_high=101.5,
    )

    target = find_liquidity_target(
        quiet_rows(),
        100.0,
        Action.LONG,
        structure=structure,
    )

    assert target is not None
    assert target.kind == "previous_day_high"
    assert target.price == 101.5


def test_day_and_previous_day_extremes_are_short_liquidity_targets() -> None:
    structure = MarketStructure(
        levels=[
            StructuralLevel(
                kind="previous_day_low",
                low=98.5,
                high=98.5,
                touches=1,
                timeframe="1D",
                score=0.88,
            ),
            StructuralLevel(
                kind="day_low",
                low=98.0,
                high=98.0,
                touches=1,
                timeframe="1D",
                score=0.92,
            ),
        ],
        day_low=98.0,
        previous_day_low=98.5,
    )

    target = find_liquidity_target(
        quiet_rows(),
        100.0,
        Action.SHORT,
        structure=structure,
    )

    assert target is not None
    assert target.kind == "previous_day_low"
    assert target.price == 98.5
