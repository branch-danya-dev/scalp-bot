from scalp_bot.domain import Action, Candle
from scalp_bot.strategy.liquidity import find_liquidity_target, find_liquidity_targets
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel


def candle(i: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(i * 60_000, o, h, l, c, 100, 10_000)


def test_consumed_isolated_swings_are_not_untouched_liquidity_in_trading_profile():
    for side in (Action.LONG, Action.SHORT):
        rows = [candle(i, 100, 102 if i == 5 else 103 if i == 15 else 100.1, 99.9, 100)
                for i in range(30)]
        if side == Action.SHORT:
            rows = [candle(i, 100, 200-c.low, 200-c.high, 100) for i, c in enumerate(rows)]
        entry, old, fresh = (101.5, 102, 103) if side == Action.LONG else (98.5, 98, 97)
        historical = find_liquidity_targets(rows, entry, side, min_distance_pct=0)
        assert historical[0].price == old
        causal = find_liquidity_targets(rows, entry, side, min_distance_pct=0, unconsumed_swings_only=True)
        assert causal[0].price == fresh
        # A confirmed structural zone is still an obstacle even if traversed.
        structure = MarketStructure(levels=[StructuralLevel(kind="resistance" if side == Action.LONG else "support",
            low=old, high=old, touches=4, timeframe="1m", score=.8)])
        protected = find_liquidity_targets(rows, entry, side, min_distance_pct=0,
            unconsumed_swings_only=True, structure=structure)
        assert protected[0].price == old and protected[0].touches == 4


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


def test_structural_zone_targets_near_edge_not_center() -> None:
    long_structure = MarketStructure(
        levels=[
            StructuralLevel(
                kind="resistance",
                low=101.0,
                high=102.0,
                touches=4,
                timeframe="15m",
                score=0.9,
            ),
        ],
    )
    long_target = find_liquidity_target(
        quiet_rows(),
        100.0,
        Action.LONG,
        structure=long_structure,
    )
    assert long_target is not None
    assert long_target.price == 101.0

    short_structure = MarketStructure(
        levels=[
            StructuralLevel(
                kind="support",
                low=98.0,
                high=99.0,
                touches=4,
                timeframe="15m",
                score=0.9,
            ),
        ],
    )
    short_target = find_liquidity_target(
        quiet_rows(),
        100.0,
        Action.SHORT,
        structure=short_structure,
    )
    assert short_target is not None
    assert short_target.price == 99.0
