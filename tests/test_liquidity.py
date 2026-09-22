from scalp_bot.domain import Action, Candle
from scalp_bot.strategy.liquidity import find_liquidity_target


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
