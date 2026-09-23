import pytest

from scalp_bot.domain import Action, Candle, OrderBook, TradeTick, Trend
from scalp_bot.strategy import (
    FormingCandleContext,
    LevelBreakoutStrategy,
    WeakLevelRejectionStrategy,
)
from scalp_bot.strategy.structure import MarketStructure, StructuralLevel
from types import SimpleNamespace


def candle(
    i: int,
    o: float,
    h: float,
    l: float,
    c: float,
    volume: float = 100,
) -> Candle:
    return Candle(i * 60_000, o, h, l, c, volume, volume * c)


def mature_breakout_candles() -> list[Candle]:
    rows: list[Candle] = []
    peaks = {
        12: 100.00,
        24: 100.04,
        36: 99.99,
        48: 100.03,
        60: 100.01,
        68: 100.02,
    }
    for i in range(80):
        if i < 74:
            base = 98.9 + (i % 8) * 0.035
            high = peaks.get(i, base + 0.18)
            close = min(base + 0.06, high - 0.03)
            rows.append(candle(i, base, high, base - 0.16, close, 120))
        else:
            closes = [99.70, 99.80, 99.88, 99.95, 100.08, 100.18]
            close = closes[i - 74]
            rows.append(
                candle(
                    i,
                    close - 0.04,
                    close + 0.06,
                    close - 0.14,
                    close,
                    300,
                )
            )
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


def far_buy_flow() -> list[TradeTick]:
    start = 30_000_000
    rows = [
        TradeTick(start - 15_000 + i * 1_500, 98.00, 1, "Sell")
        for i in range(7)
    ]
    rows += [
        TradeTick(start + i * 200, 102.00, 8, "Buy")
        for i in range(24)
    ]
    return rows


def mature_structure(generation: str = "R:100:g1") -> MarketStructure:
    return MarketStructure(
        levels=[
            StructuralLevel(
                kind="resistance",
                low=99.98,
                high=100.05,
                touches=6,
                timeframe="5m",
                score=0.86,
                reaction_pct=0.006,
                volume_ratio=1.25,
                last_touch_index=68,
                level_id="R:100",
                generation_id=generation,
                distinct_approaches=5,
                dwell_bars=9,
                acceptance_bars=3,
                failed_breaks=1,
                sweeps=1,
                lifecycle="worked",
            )
        ]
    )


def test_breakout_shared_level_requires_mature_lifecycle() -> None:
    strategy = LevelBreakoutStrategy()
    structure = mature_structure()
    structure.levels[0].distinct_approaches = 2
    structure.levels[0].lifecycle = "tested"

    decision = strategy.evaluate(
        mature_breakout_candles(),
        OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)]),
        Trend.UP,
        symbol="IMMATUREUSDT",
        trades=aggressive_buy_flow(),
        structure=structure,
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "search"


def test_breakout_shared_generation_is_used_only_once() -> None:
    strategy = LevelBreakoutStrategy()
    strategy.staged_entries_enabled = False
    rows = mature_breakout_candles()
    book = OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)])
    structure = mature_structure()

    first = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="BREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=structure,
    )
    assert first.action == Action.WAIT
    strategy._states["BREAKUSDT"].break_started_at -= 4

    entry = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="BREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=structure,
    )
    assert entry.action == Action.LONG
    assert entry.details["levelLifecycle"]["generation_id"] == "R:100:g1"
    assert entry.details["qualityFactors"]["distinctApproaches"] > 0
    assert entry.details["qualityFactors"]["dwell"] > 0

    strategy.mark_opened("BREAKUSDT", entry)
    second = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="BREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=structure,
    )
    assert second.action == Action.WAIT
    assert second.details["alreadyUsed"] is True

    new_generation = mature_structure("R:100:g2")
    third = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="BREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=new_generation,
    )
    assert third.action == Action.WAIT
    strategy._states["BREAKUSDT"].break_started_at -= 4
    third_entry = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="BREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=new_generation,
    )
    assert third_entry.action == Action.LONG


def test_breakout_forming_candle_can_supply_one_early_pressure_point() -> None:
    strategy = LevelBreakoutStrategy()
    strategy._pressure_score = lambda *args, **kwargs: (
        2,
        {"fixtureBaseScore": 2},
    )
    forming = FormingCandleContext(
        start_ms=30_000_000,
        observed_at_ms=30_010_000,
        age_seconds=10.0,
        progress_ratio=1 / 6,
        open=99.90,
        high=100.04,
        low=99.89,
        close=100.03,
        volume=40.0,
        turnover=4_001.2,
        body_pct=0.0013013,
        range_pct=0.0015015,
        body_to_range=0.866,
        upper_wick_pct=0.0001001,
        lower_wick_pct=0.0001001,
        close_position=0.933,
        volume_pace_ratio=1.4,
        range_expansion_ratio=1.1,
        velocity_bps_per_second=1.30,
        direction=Trend.UP,
    )
    context = SimpleNamespace(
        local_regime=None,
        forming_candle=forming,
    )

    decision = strategy.evaluate(
        mature_breakout_candles(),
        OrderBook(bids=[(99.99, 50)], asks=[(100.01, 50)]),
        Trend.UP,
        symbol="PRESTATEBREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=mature_structure(),
        market_context=context,
        observed_at_ms=30_010_000,
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "armed"
    assert decision.details["pressureScore"] == 3
    assert decision.details["pressure"]["formingPressure"] is True
    assert decision.details["opportunityArm"]["source"] == (
        "breakout_pressure_armed"
    )


def rejection_candles() -> list[Candle]:
    rows: list[Candle] = []
    for i in range(45):
        p = 101.5 + (i % 6) * 0.04
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


def buy_flow() -> list[TradeTick]:
    start = 20_000_000
    rows = [
        TradeTick(start - 15_000 + i * 1_500, 100.00, 1, "Sell")
        for i in range(7)
    ]
    rows += [
        TradeTick(start + i * 200, 99.95, 1, "Sell")
        for i in range(3)
    ]
    rows += [
        TradeTick(start + 1_000 + i * 200, 100.10, 3, "Buy")
        for i in range(20)
    ]
    return rows


def young_support(generation: str = "S:100:g1") -> MarketStructure:
    return MarketStructure(
        levels=[
            StructuralLevel(
                kind="support",
                low=99.96,
                high=100.05,
                touches=2,
                timeframe="1m",
                score=0.72,
                reaction_pct=0.003,
                volume_ratio=1.10,
                last_touch_index=48,
                level_id="S:100",
                generation_id=generation,
                distinct_approaches=2,
                dwell_bars=2,
                acceptance_bars=1,
                failed_breaks=1,
                sweeps=1,
                lifecycle="tested",
            )
        ]
    )


def test_rejection_shared_level_requires_fresh_lifecycle() -> None:
    strategy = WeakLevelRejectionStrategy()
    structure = young_support()
    level = structure.levels[0]
    level.distinct_approaches = 4
    level.acceptance_bars = 6
    level.lifecycle = "worked"

    decision = strategy.evaluate(
        rejection_candles(),
        OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)]),
        Trend.UP,
        symbol="OLDUSDT",
        trades=buy_flow(),
        structure=structure,
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "search"


def test_rejection_shared_generation_is_used_only_once() -> None:
    strategy = WeakLevelRejectionStrategy()
    rows = rejection_candles()
    book = OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)])
    structure = young_support()

    first = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="REJECTUSDT",
        trades=buy_flow(),
        structure=structure,
    )
    assert first.action == Action.LONG
    assert first.details["levelGeneration"] == "S:100:g1"
    assert first.details["qualityFactors"]["cleanAcceptance"] > 0
    assert first.details["qualityFactors"]["rejectionHistory"] > 0

    second = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="REJECTUSDT",
        trades=buy_flow(),
        structure=structure,
    )
    assert second.action == Action.LONG

    strategy.mark_opened("REJECTUSDT", first)
    consumed = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="REJECTUSDT",
        trades=buy_flow(),
        structure=structure,
    )
    assert consumed.action == Action.WAIT
    assert consumed.details["alreadyUsed"] is True

    new_generation = young_support("S:100:g2")
    third = strategy.evaluate(
        rows,
        book,
        Trend.UP,
        symbol="REJECTUSDT",
        trades=buy_flow(),
        structure=new_generation,
    )
    assert third.action == Action.LONG



def test_breakout_global_flow_away_from_level_does_not_confirm() -> None:
    strategy = LevelBreakoutStrategy()
    decision = strategy.evaluate(
        mature_breakout_candles(),
        OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)]),
        Trend.UP,
        symbol="FARFLOWUSDT",
        trades=far_buy_flow(),
        structure=mature_structure(),
    )

    assert decision.action == Action.WAIT
    assert decision.details["state"] == "break"
    assert decision.details["flow"]["imbalance5s"] > 0
    assert decision.details["levelFlow"]["tradeCount"] == 0


def test_breakout_records_level_flow_on_entry() -> None:
    strategy = LevelBreakoutStrategy()
    strategy.staged_entries_enabled = False
    book = OrderBook(bids=[(100.16, 50)], asks=[(100.17, 50)])
    first = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="LOCALFLOWUSDT",
        trades=aggressive_buy_flow(),
        structure=mature_structure(),
    )
    assert first.action == Action.WAIT
    strategy._states["LOCALFLOWUSDT"].break_started_at -= 4
    decision = strategy.evaluate(
        mature_breakout_candles(),
        book,
        Trend.UP,
        symbol="LOCALFLOWUSDT",
        trades=aggressive_buy_flow(),
        structure=mature_structure(),
    )

    assert decision.action == Action.LONG
    assert decision.details["levelFlow"]["tradeCount"] > 0
    assert decision.details["levelFlow"]["imbalance"] > 0



def test_rejection_far_global_flow_cannot_upgrade_absorption_probe_to_reaction() -> None:
    strategy = WeakLevelRejectionStrategy()
    decision = strategy.evaluate(
        rejection_candles(),
        OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)]),
        Trend.UP,
        symbol="FARREJECTUSDT",
        trades=[
            *[
                TradeTick(19_985_000 + i * 1_500, 100.00, 1, "Sell")
                for i in range(7)
            ],
            *[
                TradeTick(20_000_000 + i * 200, 99.95, 1, "Sell")
                for i in range(3)
            ],
            *[
                TradeTick(20_001_000 + i * 200, 102.0, 3, "Buy")
                for i in range(20)
            ],
        ],
        structure=young_support(),
    )

    # Stage 15 intentionally allows the failed-break absorption itself to
    # open only a small probe. Far-away global buys must not promote it to
    # the confirmed REACTION/add phase.
    assert decision.action == Action.LONG
    assert decision.details["state"] == "reject"
    assert decision.details["stagedEntry"]["phase"] == "probe"
    assert decision.details["flowReversed"] is False
    assert decision.details["flow"]["imbalance5s"] > 0
    assert decision.details["breakoutFlow"]["tradeCount"] > 0
    assert decision.details["levelFlow"]["imbalance"] < 0
    assert decision.details["recentLevelFlow"]["imbalance"] < 0


def test_rejection_records_level_flow_on_entry() -> None:
    strategy = WeakLevelRejectionStrategy()
    decision = strategy.evaluate(
        rejection_candles(),
        OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)]),
        Trend.UP,
        symbol="LOCALREJECTUSDT",
        trades=buy_flow(),
        structure=young_support(),
    )

    assert decision.action == Action.LONG
    assert decision.details["levelFlow"]["tradeCount"] > 0
    assert decision.details["levelFlow"]["buyNotional"] > 0


def test_rejection_countertrend_signal_is_observed_but_not_tradeable() -> None:
    strategy = WeakLevelRejectionStrategy()
    decision = strategy.evaluate(
        rejection_candles(),
        OrderBook(bids=[(100.09, 50)], asks=[(100.10, 50)]),
        Trend.DOWN,
        symbol="COUNTERREJECTUSDT",
        trades=buy_flow(),
        structure=young_support(),
    )

    assert decision.action == Action.WAIT
    assert decision.details["trendAligned"] is False
    assert decision.details["rejectedAction"] == "long"



def test_breakout_armed_hypothesis_pins_market_object_until_fire(
    monkeypatch,
) -> None:
    strategy = LevelBreakoutStrategy()
    strategy._pressure_score = lambda *args, **kwargs: (
        3,
        {"fixtureBaseScore": 3},
    )
    rows = mature_breakout_candles()
    market = OrderBook(
        bids=[(99.99, 50)],
        asks=[(100.01, 50)],
    )
    structure = mature_structure()

    armed = strategy.evaluate(
        rows,
        market,
        Trend.UP,
        symbol="PINNEDBREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=structure,
        observed_at_ms=30_010_000,
    )

    assert armed.action == Action.WAIT
    assert armed.details["state"] == "armed"
    prepared = armed.details["preparedOpportunity"]
    assert prepared["pinned"] is True
    pinned_level = prepared["watchedLevel"]

    def selector_must_not_run(*args, **kwargs):
        raise AssertionError(
            "ARMED fast path must reuse the prepared zone"
        )

    monkeypatch.setattr(
        strategy,
        "_select_zone",
        selector_must_not_run,
    )

    still_armed = strategy.evaluate(
        rows,
        market,
        Trend.UP,
        symbol="PINNEDBREAKUSDT",
        trades=aggressive_buy_flow(),
        structure=structure,
        observed_at_ms=30_010_200,
    )

    assert still_armed.action == Action.WAIT
    assert still_armed.details["state"] == "armed"
    assert (
        still_armed.details["preparedOpportunity"]["watchedLevel"]
        == pytest.approx(pinned_level)
    )
