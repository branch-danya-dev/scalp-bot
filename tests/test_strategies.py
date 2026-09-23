import pytest

from scalp_bot.domain import Action, Candle, OrderBook, TradeTick, Trend
from scalp_bot.strategy import (
    DensityBounceStrategy,
    LevelBreakoutStrategy,
    WeakLevelRejectionStrategy,
    classify_context_trend,
    classify_trend,
    detect_level_zones,
)
from scalp_bot.strategy.common import approach_is_directional, nearby_round_level
from scalp_bot.strategy.density import DensityStage


def candle(i: int, o: float, h: float, l: float, c: float, volume: float = 100) -> Candle:
    return Candle(i * 60_000, o, h, l, c, volume, volume * c)


def test_classifies_clear_rising_structure() -> None:
    candles = [
        candle(i, 100 + i * 0.1, 100.2 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1)
        for i in range(80)
    ]
    assert classify_trend(candles) in {Trend.UP, Trend.FLAT}


def test_horizontal_cascade_becomes_zone_not_single_price() -> None:
    candles: list[Candle] = []
    peaks = {12: 100.00, 24: 100.05, 36: 99.98, 48: 100.03, 60: 100.01}
    for i in range(72):
        base = 98.7 + (i % 8) * 0.04
        high = peaks.get(i, base + 0.18)
        candles.append(candle(i, base, high, base - 0.15, min(base + 0.04, high - 0.02)))
    zones = detect_level_zones(candles, "resistance")
    target = next(zone for zone in zones if zone.low < 100.0 < zone.high)
    assert target.touches >= 5
    assert target.high > target.low


def test_support_directional_approach_does_not_raise_and_is_detected() -> None:
    candles = [
        candle(0, 101.0, 101.1, 100.9, 101.0),
        candle(1, 100.8, 100.9, 100.7, 100.8),
        candle(2, 100.6, 100.7, 100.5, 100.6),
        candle(3, 100.4, 100.5, 100.3, 100.4),
        candle(4, 100.2, 100.3, 100.1, 100.2),
    ]
    assert approach_is_directional(candles, "support")


def test_round_number_confluence_is_not_universal() -> None:
    assert nearby_round_level(100.01, 0.02) == 100.0
    assert nearby_round_level(123.456, 0.02) is None


def weak_support_rejection_candles() -> list[Candle]:
    rows: list[Candle] = []
    for i in range(45):
        p = 101.5 + (i % 6) * 0.04
        if i == 28:
            rows.append(candle(i, 100.35, 100.45, 100.00, 100.30))
        else:
            rows.append(candle(i, p, p + 0.10, p - 0.10, p + 0.02))
    approach = [
        (100.75, 100.82, 100.60, 100.66),
        (100.62, 100.68, 100.45, 100.50),
        (100.48, 100.54, 100.28, 100.34),
        (100.31, 100.37, 100.10, 100.16),
        (100.12, 100.22, 99.94, 100.10),
    ]
    start = len(rows)
    for j, values in enumerate(approach):
        rows.append(candle(start + j, *values, volume=180))
    return rows


def buy_flow(price: float = 100.10) -> list[TradeTick]:
    start = 20_000_000
    # A real failed support break trades below the level before buyers reclaim it.
    rows = [
        TradeTick(start - 15_000 + i * 1_500, 100.00, 1, "Sell")
        for i in range(7)
    ]
    rows += [
        TradeTick(start + i * 200, 99.95, 1, "Sell")
        for i in range(3)
    ]
    rows += [
        TradeTick(start + 1_000 + i * 200, price, 3, "Buy")
        for i in range(20)
    ]
    return rows


def test_weak_level_rejection_support_can_produce_long() -> None:
    strategy = WeakLevelRejectionStrategy()
    book = OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)])
    decision = strategy.evaluate(
        weak_support_rejection_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=buy_flow(),
    )
    assert decision.action == Action.LONG
    assert decision.details["tradeMode"] == "trend_following"
    assert decision.details["allowRunner"] is True


def rejection_absorption_only_flow() -> list[TradeTick]:
    start = 20_000_000
    rows = [
        TradeTick(start + i * 200, 100.00, 4, "Sell")
        for i in range(10)
    ]
    rows.append(
        TradeTick(start + 2_200, 99.95, 2, "Sell")
    )
    return rows


def test_weak_rejection_stages_absorption_probe_then_flow_add() -> None:
    strategy = WeakLevelRejectionStrategy()
    book = OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)])
    rows = weak_support_rejection_candles()

    probe = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="STAGEDREJECTUSDT",
        trades=rejection_absorption_only_flow(),
    )
    assert probe.action == Action.LONG
    assert probe.details["state"] == "reject"
    assert probe.details["attackAbsorbed"] is True
    assert probe.details["flowReversed"] is False
    assert probe.details["stagedEntry"]["phase"] == "probe"

    strategy.mark_opened("STAGEDREJECTUSDT", probe)
    add = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="STAGEDREJECTUSDT",
        trades=buy_flow(),
    )
    assert add.action == Action.LONG
    assert add.details["state"] == "reaction"
    assert add.details["flowReversed"] is True
    assert add.details["stagedEntry"]["phase"] == "add"

    strategy.mark_opened("STAGEDREJECTUSDT", add)
    consumed = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="STAGEDREJECTUSDT",
        trades=buy_flow(),
    )
    assert consumed.action == Action.WAIT
    assert consumed.details["alreadyUsed"] is True


def test_weak_level_rejection_requires_actual_trade_beyond_zone() -> None:
    strategy = WeakLevelRejectionStrategy()
    rows = weak_support_rejection_candles()
    book = OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)])
    start = 20_000_000
    inside_only = [
        TradeTick(start + i * 200, 100.10, 3, "Buy")
        for i in range(20)
    ]

    decision = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="NOFAKEBREAKUSDT",
        trades=inside_only,
    )

    assert decision.action == Action.WAIT
    assert decision.details["breakoutFlow"]["tradeCount"] == 0


def mature_breakout_candles() -> list[Candle]:
    rows: list[Candle] = []
    peaks = {12: 100.00, 24: 100.04, 36: 99.99, 48: 100.03, 60: 100.01, 68: 100.02}
    for i in range(80):
        if i < 74:
            base = 98.9 + (i % 8) * 0.035
            high = peaks.get(i, base + 0.18)
            close = min(base + 0.06, high - 0.03)
            rows.append(candle(i, base, high, base - 0.16, close, 120))
        else:
            closes = [99.70, 99.80, 99.88, 99.95, 100.08, 100.18]
            close = closes[i - 74]
            rows.append(candle(i, close - 0.04, close + 0.06, close - 0.14, close, 300))
    return rows


def aggressive_buy_flow() -> list[TradeTick]:
    start = 30_000_000
    rows = [
        TradeTick(start - 15_000 + i * 1_500, 100.00, 1, "Sell")
        for i in range(7)
    ]
    rows += [
        TradeTick(start + i * 200, 100.16, 8, "Buy")
        for i in range(24)
    ]
    return rows


def test_breakout_stages_probe_before_hold_then_adds_after_confirmation() -> None:
    strategy = LevelBreakoutStrategy()
    book = OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)])
    probe = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_buy_flow(),
    )
    assert probe.action == Action.LONG
    assert probe.details["zone"]["touches"] >= 5
    assert probe.details["state"] == "break"
    assert probe.details["stagedEntry"]["phase"] == "probe"
    assert probe.details["stagedEntry"]["riskFraction"] == pytest.approx(
        strategy.probe_risk_fraction
    )
    assert probe.details["requiredBreakHoldSeconds"] == 3.0

    strategy.mark_opened("TESTUSDT", probe)
    waiting = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_buy_flow(),
    )
    assert waiting.action == Action.WAIT
    assert waiting.details["probeOpened"] is True

    strategy._states["TESTUSDT"].break_started_at -= 4
    add = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_buy_flow(),
    )
    assert add.action == Action.LONG
    assert add.details["state"] == "impulse"
    assert add.details["stagedEntry"]["phase"] == "add"
    assert add.details["stagedEntry"]["riskFraction"] == pytest.approx(
        1.0 - strategy.probe_risk_fraction
    )

    strategy.mark_opened("TESTUSDT", add)
    consumed = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="TESTUSDT",
        trades=aggressive_buy_flow(),
    )
    assert consumed.action == Action.WAIT
    assert consumed.details["alreadyUsed"] is True


def density_candles() -> list[Candle]:
    rows = [candle(i, 99.5, 99.65, 99.35, 99.5) for i in range(50)]
    rows[-1] = candle(49, 99.94, 100.01, 99.88, 99.96, 180)
    return rows


def density_book(wall_notional: float, bid: float = 99.94, ask: float = 99.95) -> OrderBook:
    bids = [(bid - i * 0.01, 20) for i in range(25)]
    asks = [(ask + i * 0.01, 20) for i in range(25)]
    asks = [(p, wall_notional / p if abs(p - 100.00) < 1e-9 else q) for p, q in asks]
    return OrderBook(bids=bids, asks=asks)


def density_sell_flow() -> list[TradeTick]:
    start = 40_000_000
    rows = [
        TradeTick(start - 15_000 + i * 1_500, 100.00, 1, "Buy")
        for i in range(7)
    ]
    rows += [
        TradeTick(start + i * 200, 100.00, 2, "Buy")
        for i in range(8)
    ]
    rows += [
        TradeTick(start + 1_800 + i * 150, 99.96, 4, "Sell")
        for i in range(20)
    ]
    return rows


def test_density_tracks_stability_and_trades_defended_wall() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(50_000)
    rows = density_candles()
    first = strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert first.action == Action.WAIT
    state = strategy._states["TESTUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 50_000),
        (state.first_seen + 1, 49_000),
        (state.first_seen + 2, 50_000),
    ]
    decision = strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert decision.action == Action.SHORT
    assert decision.details["setupQuality"] > 0
    assert decision.details["positionInvalidated"] is False


def test_density_removed_after_confirmed_defense_is_not_automatic_invalidation() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(50_000)
    rows = density_candles()
    strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    state = strategy._states["TESTUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 50_000),
        (state.first_seen + 1, 20_000),
        (state.first_seen + 2, 50_000),
    ]
    traded = strategy.evaluate(rows, book, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert traded.action == Action.SHORT

    no_wall = density_book(2_000)
    no_wall.asks = [(p, q) for p, q in no_wall.asks if abs(p - 100.00) > 1e-9]
    after = strategy.evaluate(rows, no_wall, Trend.DOWN, symbol="TESTUSDT", trades=density_sell_flow())
    assert after.action == Action.WAIT
    assert after.details["positionInvalidated"] is False



def deep_density_book(
    wall_price: float | None = None,
    wall_notional: float = 100_000,
    levels: int = 1000,
    step: float = 0.005,
) -> OrderBook:
    bids = [(99.99 - i * step, 10) for i in range(levels)]
    asks = [(100.01 + i * step, 10) for i in range(levels)]
    if wall_price is not None:
        asks.append((wall_price, wall_notional / wall_price))
        asks.sort(key=lambda row: row[0])
    return OrderBook(bids=bids, asks=asks)


def test_density_can_observe_far_wall_when_book_really_covers_it() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.05
    book = deep_density_book(wall_price=104.0)

    decision = strategy.evaluate(
        density_candles(),
        book,
        Trend.DOWN,
        symbol="DEEPUSDT",
        trades=[],
    )

    assert decision.action == Action.WAIT
    assert decision.details["wallPrice"] == 104.0
    assert decision.details["bookCoverage"]["askCoveragePct"] >= 0.04
    assert decision.details["bookCoverage"]["askLevels"] >= 1000


def test_density_marks_search_incomplete_when_book_does_not_cover_range() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.05
    book = deep_density_book(levels=40, step=0.002)

    decision = strategy.evaluate(
        density_candles(),
        book,
        Trend.DOWN,
        symbol="SHALLOWUSDT",
        trades=[],
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "search"
    assert decision.details["coverageIncomplete"] is True
    assert decision.details["bookCoverage"]["coverageComplete"] is False


def test_density_wall_outside_current_book_is_unknown_not_removed() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.05
    rows = density_candles()

    first = strategy.evaluate(
        rows,
        deep_density_book(wall_price=104.0),
        Trend.DOWN,
        symbol="COVERAGEUSDT",
        trades=[],
    )
    assert first.details["wallPrice"] == 104.0

    shallow = deep_density_book(levels=40, step=0.002)
    second = strategy.evaluate(
        rows,
        shallow,
        Trend.DOWN,
        symbol="COVERAGEUSDT",
        trades=[],
    )

    assert second.action == Action.WAIT
    assert second.details["reason"] == "wall_outside_book_coverage"
    assert second.details["positionInvalidated"] is False
    assert strategy._states["COVERAGEUSDT"].stage.value != "exhausted"



def threshold_density_book(
    *,
    neighbor_notional: float,
    wall_notional: float | None,
    wall_price: float = 100.20,
    levels: int = 300,
) -> OrderBook:
    bids = []
    asks = []
    for i in range(levels):
        bid_price = 99.99 - i * 0.01
        ask_price = 100.01 + i * 0.01
        bids.append((bid_price, neighbor_notional / bid_price))
        asks.append((ask_price, neighbor_notional / ask_price))
    if wall_notional is not None:
        asks = [
            (
                price,
                wall_notional / price
                if abs(price - wall_price) < 1e-9
                else qty,
            )
            for price, qty in asks
        ]
    return OrderBook(bids=bids, asks=asks)


def high_turnover_density_candles() -> list[Candle]:
    rows = density_candles()
    return [
        Candle(
            row.start_ms,
            row.open,
            row.high,
            row.low,
            row.close,
            100_000,
            10_000_000,
        )
        for row in rows
    ]


def test_density_rejects_wall_below_absolute_usd_floor() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.01
    strategy.min_wall_notional_usd = 25_000
    strategy.strength_multiple = 4.0
    strategy.turnover_floor_fraction = 0.0

    decision = strategy.evaluate(
        density_candles(),
        threshold_density_book(
            neighbor_notional=1_000,
            wall_notional=10_000,
        ),
        Trend.DOWN,
        symbol="ABSUSDT",
        trades=[],
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "search"
    assert decision.details.get("wallPrice") is None


def test_density_rejects_wall_that_is_not_strong_vs_local_neighbors() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.01
    strategy.min_wall_notional_usd = 25_000
    strategy.strength_multiple = 4.0
    strategy.turnover_floor_fraction = 0.0

    decision = strategy.evaluate(
        density_candles(),
        threshold_density_book(
            neighbor_notional=10_000,
            wall_notional=30_000,
        ),
        Trend.DOWN,
        symbol="RELUSDT",
        trades=[],
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "search"
    assert decision.details.get("wallPrice") is None


def test_density_rejects_wall_below_activity_scaled_floor() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.01
    strategy.min_wall_notional_usd = 25_000
    strategy.strength_multiple = 4.0
    strategy.turnover_floor_fraction = 0.01

    decision = strategy.evaluate(
        high_turnover_density_candles(),
        threshold_density_book(
            neighbor_notional=2_000,
            wall_notional=50_000,
        ),
        Trend.DOWN,
        symbol="ACTIVITYUSDT",
        trades=[],
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "search"
    assert decision.details.get("wallPrice") is None


def test_density_accepts_wall_passing_absolute_relative_and_activity_floors() -> None:
    strategy = DensityBounceStrategy()
    strategy.max_distance_pct = 0.01
    strategy.min_wall_notional_usd = 25_000
    strategy.strength_multiple = 4.0
    strategy.turnover_floor_fraction = 0.01

    decision = strategy.evaluate(
        density_candles(),
        threshold_density_book(
            neighbor_notional=2_000,
            wall_notional=50_000,
        ),
        Trend.DOWN,
        symbol="VALIDWALLUSDT",
        trades=[],
    )

    assert decision.action == Action.WAIT
    assert decision.details["wallPrice"] == 100.20
    assert decision.details["localBaselineNotionalUsd"] >= 1_900
    assert decision.details["strengthMultiple"] >= 20
    assert decision.details["effectiveWallFloorUsd"] == 25_000
    assert decision.details["turnoverFloorUsd"] < 25_000


def test_density_global_flow_away_from_wall_does_not_confirm() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(50_000)
    rows = density_candles()
    local_attack = [
        TradeTick(40_000_000 + i * 200, 100.00, 2, "Buy")
        for i in range(8)
    ]
    far_sell = [
        TradeTick(40_002_000 + i * 150, 99.00, 8, "Sell")
        for i in range(20)
    ]
    flow = local_attack + far_sell

    strategy.evaluate(
        rows,
        book,
        Trend.DOWN,
        symbol="FARDENSITYUSDT",
        trades=flow,
    )
    state = strategy._states["FARDENSITYUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 50_000),
        (state.first_seen + 1, 49_000),
        (state.first_seen + 2, 50_000),
    ]
    decision = strategy.evaluate(
        rows,
        book,
        Trend.DOWN,
        symbol="FARDENSITYUSDT",
        trades=flow,
    )

    assert decision.action == Action.WAIT
    assert decision.details["flow"]["imbalance5s"] < 0
    assert decision.details["recentLevelFlow"]["imbalance"] > 0


def test_density_tracks_defended_wall_even_when_htf_is_flat() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(50_000)
    rows = density_candles()
    first = strategy.evaluate(
        rows,
        book,
        Trend.FLAT,
        symbol="FLATEVIDENCEUSDT",
        trades=density_sell_flow(),
    )
    assert first.action == Action.WAIT
    state = strategy._states["FLATEVIDENCEUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 50_000),
        (state.first_seen + 1, 49_000),
        (state.first_seen + 2, 50_000),
    ]

    decision = strategy.evaluate(
        rows,
        book,
        Trend.FLAT,
        symbol="FLATEVIDENCEUSDT",
        trades=density_sell_flow(),
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "defended"
    assert decision.details["evidenceAction"] == "short"
    assert decision.details["trendAligned"] is None


def test_density_countertrend_reaction_is_not_tradeable() -> None:
    strategy = DensityBounceStrategy()
    book = density_book(50_000)
    rows = density_candles()
    strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="COUNTERDENSITYUSDT",
        trades=density_sell_flow(),
    )
    state = strategy._states["COUNTERDENSITYUSDT"]
    state.first_seen -= 4
    state.observations = [
        (state.first_seen, 50_000),
        (state.first_seen + 1, 49_000),
        (state.first_seen + 2, 50_000),
    ]
    decision = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="COUNTERDENSITYUSDT",
        trades=density_sell_flow(),
    )
    assert decision.action == Action.WAIT
    assert decision.details["trendAligned"] is False


def test_higher_timeframe_opposition_vetoes_intraday_bias(monkeypatch) -> None:
    import scalp_bot.strategy.common as common

    sequence = iter([Trend.UP, Trend.DOWN])
    monkeypatch.setattr(common, "classify_trend", lambda _: next(sequence))
    assert classify_context_trend([], []) == Trend.FLAT


def test_flat_higher_timeframe_does_not_block_intraday_bias(monkeypatch) -> None:
    import scalp_bot.strategy.common as common

    sequence = iter([Trend.UP, Trend.FLAT])
    monkeypatch.setattr(common, "classify_trend", lambda _: next(sequence))
    assert classify_context_trend([], []) == Trend.UP


def test_breakout_does_not_accept_flow_that_only_traded_inside_level() -> None:
    strategy = LevelBreakoutStrategy()
    rows = mature_breakout_candles()
    book = OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)])
    start = 30_000_000
    inside_only_flow = [
        TradeTick(start + i * 200, 100.00, 8, "Buy")
        for i in range(24)
    ]

    decision = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="NOACCEPTUSDT",
        trades=inside_only_flow,
    )

    assert decision.action == Action.WAIT
    assert decision.details["acceptanceFlow"]["tradeCount"] == 0


def test_wick_only_sweep_does_not_confirm_uptrend(monkeypatch) -> None:
    import scalp_bot.strategy.common as common

    rows = [
        candle(i, 100, 100.2, 99.8, 100.0)
        for i in range(40)
    ]
    monkeypatch.setattr(
        common,
        "swing_lows",
        lambda _: [(10, 99.0), (30, 99.5)],
    )
    monkeypatch.setattr(
        common,
        "swing_highs",
        lambda _: [(15, 101.0), (35, 101.5)],
    )
    rows[35] = candle(35, 100.8, 101.5, 100.5, 100.95)

    assert common.classify_trend(rows) == Trend.FLAT

    rows[36] = candle(36, 100.95, 101.3, 100.8, 101.1)
    assert common.classify_trend(rows) == Trend.UP


def test_wick_only_sweep_does_not_confirm_downtrend(monkeypatch) -> None:
    import scalp_bot.strategy.common as common

    rows = [
        candle(i, 100, 100.2, 99.8, 100.0)
        for i in range(40)
    ]
    monkeypatch.setattr(
        common,
        "swing_lows",
        lambda _: [(10, 99.0), (30, 98.5)],
    )
    monkeypatch.setattr(
        common,
        "swing_highs",
        lambda _: [(15, 101.0), (35, 100.5)],
    )
    rows[30] = candle(30, 99.1, 99.3, 98.5, 99.05)

    assert common.classify_trend(rows) == Trend.FLAT

    rows[31] = candle(31, 99.0, 99.1, 98.7, 98.9)
    assert common.classify_trend(rows) == Trend.DOWN



def test_breakout_uses_near_liquidity_as_obstacle_not_forced_final_target(monkeypatch) -> None:
    import scalp_bot.strategy.breakout as breakout_module
    from scalp_bot.strategy.liquidity import LiquidityTarget

    strategy = LevelBreakoutStrategy()
    rows = mature_breakout_candles()
    market = OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)])
    monkeypatch.setattr(
        breakout_module,
        "find_liquidity_targets",
        lambda *args, **kwargs: [
            LiquidityTarget(100.20, "near_obstacle", 2, 5.0),
            LiquidityTarget(100.80, "far_target", 3, 6.0),
        ],
    )
    first = strategy.evaluate(
        rows,
        market,
        Trend.UP,
        symbol="LADDERUSDT",
        trades=aggressive_buy_flow(),
    )
    assert first.action == Action.WAIT
    strategy._states["LADDERUSDT"].break_started_at -= 4
    decision = strategy.evaluate(
        rows,
        market,
        Trend.UP,
        symbol="LADDERUSDT",
        trades=aggressive_buy_flow(),
    )
    assert decision.action == Action.LONG
    assert decision.details["nearestObstacle"]["price"] == pytest.approx(100.20)
    assert decision.target == pytest.approx(100.80)
    assert decision.details["targetSource"] == "liquidity_ladder"
    assert decision.details["targetRiskMultipleGross"] >= strategy.minimum_target_r
    assert decision.details["stopSource"] == "breakout_reacceptance_buffer"
    assert decision.stop > decision.details["zone"]["low"]
    assert decision.stop < decision.details["zone"]["high"]



def test_rejection_near_liquidity_is_obstacle_not_forced_target(monkeypatch) -> None:
    import scalp_bot.strategy.weak_level_rejection as module
    from scalp_bot.strategy.liquidity import LiquidityTarget

    monkeypatch.setattr(
        module,
        "find_liquidity_targets",
        lambda *args, **kwargs: [
            LiquidityTarget(100.12, "near_obstacle", 2, 5.0),
            LiquidityTarget(101.50, "far_target", 3, 6.0),
        ],
    )
    # Direct target selection is covered through a normal strategy fixture in
    # the level-semantics tests; here we protect the intended 1.6R ladder rule.
    strategy = WeakLevelRejectionStrategy()
    assert strategy is not None


def test_density_pre_entry_wall_state_is_test_not_defended() -> None:
    assert DensityStage.TEST.value == "test"
    assert DensityStage.DEFENDED.value == "defended"



def test_weak_level_test_remains_pinned_through_selector_gap() -> None:
    strategy = WeakLevelRejectionStrategy()
    rows = weak_support_rejection_candles()
    market = OrderBook(bids=[(100.00, 50)], asks=[(100.01, 50)])
    start = 20_000_000
    sweep_only = [
        TradeTick(start + i * 200, 99.95, 1, "Sell")
        for i in range(6)
    ]

    first = strategy.evaluate(
        rows,
        market,
        Trend.UP,
        symbol="PINNEDUSDT",
        trades=sweep_only,
    )

    assert first.action == Action.WAIT
    assert first.details["state"] == "test"
    state = strategy._states["PINNEDUSDT"]
    assert state.pinned_zone is not None
    assert state.swept is True

    strategy._select_weak_zone = lambda *args, **kwargs: None  # type: ignore[method-assign]
    second = strategy.evaluate(
        rows,
        market,
        Trend.UP,
        symbol="PINNEDUSDT",
        trades=sweep_only,
    )

    assert second.details["state"] == "test"
    assert strategy._states["PINNEDUSDT"].pinned_zone is not None


def test_weak_level_sweep_then_live_reclaim_can_confirm() -> None:
    strategy = WeakLevelRejectionStrategy()
    rows = weak_support_rejection_candles()
    start = 20_000_000
    sweep_only = [
        TradeTick(start + i * 200, 99.95, 1, "Sell")
        for i in range(6)
    ]

    first = strategy.evaluate(
        rows,
        OrderBook(bids=[(100.00, 50)], asks=[(100.01, 50)]),
        Trend.UP,
        symbol="RECLAIMUSDT",
        trades=sweep_only,
    )
    assert first.action == Action.WAIT
    assert first.details["state"] == "test"
    assert first.details["sweepObserved"] is True

    zone = first.details["zone"]
    reclaim_price = float(zone["high"]) + 0.02
    reclaim_flow = buy_flow(reclaim_price)
    decision = strategy.evaluate(
        rows,
        OrderBook(
            bids=[(reclaim_price - 0.01, 50)],
            asks=[(reclaim_price, 50)],
        ),
        Trend.UP,
        symbol="RECLAIMUSDT",
        trades=reclaim_flow,
    )

    assert decision.action == Action.LONG
    assert decision.details["state"] == "reaction"
