import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import OrderBook, Side, TradePlan
from scalp_bot.paper import PaperBroker


def plan(symbol: str, side: Side, notional: float = 400) -> TradePlan:
    return TradePlan(
        symbol=symbol,
        strategy="test",
        side=side,
        setup_entry=100,
        market_entry=100,
        stop=99.5 if side == Side.LONG else 100.5,
        target=101 if side == Side.LONG else 99,
        notional=notional,
        leverage=0.4,
        max_loss_usd=2,
        expected_gross_profit=4,
        estimated_costs=0,
        expected_net_profit=4,
        expected_net_loss=2,
        net_reward_risk=2,
        entry_drift_pct=0,
        setup_id=f"test:{symbol}",
        strategy_details={},
    )


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook(bids=[(bid, 100)], asks=[(ask, 100)])


def test_position_can_go_negative_without_being_closed_before_stop() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0,
        max_leverage=1,
        max_open_positions=4,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG), book(99.99, 100.00))
    events = broker.mark("AAAUSDT", 99.70, book(99.69, 99.70))
    assert events == []
    assert "AAAUSDT" in broker.positions
    assert broker.positions["AAAUSDT"].mae_usd > 0

    events = broker.mark("AAAUSDT", 101.00, book(101.00, 101.01))
    assert len(events) == 1
    assert events[0]["event"] == "trade_closed"
    assert events[0]["reason"] == "target"
    assert events[0]["maeUsd"] > 0


def test_positions_are_isolated_by_symbol_but_share_exposure_budget() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        max_leverage=1,
        max_open_positions=4,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG, 400), book(99.99, 100.00))
    broker.open(plan("BBBUSDT", Side.SHORT, 400), book(100.00, 100.01))
    assert set(broker.positions) == {"AAAUSDT", "BBBUSDT"}
    assert broker.total_exposure == 800
    assert broker.available_notional == 200
    assert broker.mark("AAAUSDT", 99.8, book(99.79, 99.80)) == []
    assert broker.positions["BBBUSDT"].mae_usd == 0


def test_partial_take_locks_profit_and_moves_runner_stop_to_net_breakeven() -> None:
    cfg = Settings(
        taker_fee_rate=0.0005,
        slippage_bps=0,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        runner_target_r=2.5,
        breakeven_buffer_bps=1,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG, 1000), book(99.99, 100.00))

    events = broker.mark("AAAUSDT", 100.55, book(100.55, 100.56))
    assert len(events) == 1
    assert events[0]["event"] == "partial_take"

    pos = broker.positions["AAAUSDT"]
    assert pos.partial_taken
    assert round(pos.notional, 6) == 300
    assert pos.stop > pos.entry
    assert pos.target > 101
    assert broker.total_pnl > 0


def test_runner_reversal_after_partial_does_not_return_full_trade_to_loss() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        runner_target_r=3.0,
        breakeven_buffer_bps=0,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG, 1000), book(100.00, 100.01))
    partial = broker.mark("AAAUSDT", 100.53, book(100.53, 100.54))
    assert partial and partial[0]["event"] == "partial_take"
    locked = broker.total_pnl
    stop = broker.positions["AAAUSDT"].stop

    closed = broker.mark("AAAUSDT", stop - 0.001, book(stop - 0.001, stop))
    assert closed and closed[-1]["event"] == "trade_closed"
    assert closed[-1]["partialTaken"]
    assert closed[-1]["netPnl"] >= locked - 0.05


def test_no_follow_through_cuts_weak_loser_before_hard_stop() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=10,
        no_follow_through_max_mfe_r=0.25,
        early_cut_at_r=0.45,
    )
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG, 1000), book(99.99, 100.00))
    broker.positions["AAAUSDT"].opened_at -= 15

    events = broker.mark("AAAUSDT", 99.75, book(99.75, 99.76))
    assert len(events) == 1
    assert events[0]["event"] == "trade_closed"
    assert events[0]["reason"] == "no_follow_through"
    assert events[0]["maeR"] < 1.0


def test_current_risk_is_released_after_stop_moves_beyond_entry() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        breakeven_buffer_bps=0,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG, 1000), book(100.00, 100.01))
    assert broker.open_risk_usd > 0
    broker.mark("AAAUSDT", 100.53, book(100.53, 100.54))
    assert broker.open_risk_usd == 0



def test_countertrend_reaction_does_not_create_runner_partial() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=True,
        partial_take_at_r=1.0,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("AAAUSDT", Side.LONG, 1000)
    p.strategy_details = {"tradeMode": "countertrend_reaction", "allowRunner": False}
    broker.open(p, book(99.99, 100.00))

    events = broker.mark("AAAUSDT", 100.60, book(100.60, 100.61))

    assert events == []
    assert "AAAUSDT" in broker.positions
    assert broker.positions["AAAUSDT"].partial_taken is False



def test_research_paper_run_does_not_stop_opening_after_session_loss_cap() -> None:
    cfg = Settings(
        start_balance=1000,
        max_daily_loss_fraction=0.03,
        enforce_session_loss_limit=False,
        max_leverage=1,
    )
    broker = PaperBroker(cfg)
    broker.balance = 900

    allowed, reason = broker.can_open("AAAUSDT")

    assert allowed
    assert reason == "allowed"


def test_session_loss_limit_still_exists_when_explicitly_enabled() -> None:
    cfg = Settings(
        start_balance=1000,
        max_daily_loss_fraction=0.03,
        enforce_session_loss_limit=True,
        max_leverage=1,
    )
    broker = PaperBroker(cfg)
    broker.balance = 969

    allowed, reason = broker.can_open("AAAUSDT")

    assert not allowed
    assert reason == "session loss limit reached"



def test_maker_target_requires_trade_through_not_book_touch() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0.5,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("AAAUSDT", Side.LONG, 100)
    p.target = 101.0
    broker.open(p, book(99.99, 100.00))

    # A book-only update through the target is not sufficient evidence that
    # our resting maker sell actually filled.
    events = broker.mark(
        "AAAUSDT",
        100.90,
        book(101.01, 101.02),
        trade_price=None,
    )
    assert events == []
    assert "AAAUSDT" in broker.positions

    # A public trade through the resting limit by the configured confirmation
    # margin is the conservative paper fill model.
    events = broker.mark(
        "AAAUSDT",
        101.01,
        book(100.95, 101.05),
        trade_price=101.01,
    )
    assert events and events[-1]["reason"] == "target"
    assert events[-1]["exit"] == pytest.approx(101.0)


def test_closed_trade_counter_is_not_truncated_with_ui_history() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        max_leverage=1,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    for i in range(205):
        symbol = f"T{i}USDT"
        broker.open(plan(symbol, Side.LONG, 10), book(99.99, 100.00))
        broker.close(symbol, book(100.00, 100.01), "test")

    assert broker.total_closed_trades == 205
    assert len(broker.closed_trades) == 200



def test_portfolio_gross_exposure_cannot_exceed_ten_x() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=1.0,
        max_open_positions=4,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    broker = PaperBroker(cfg)
    market = book(99.99, 100.00)

    first = plan("AAAUSDT", Side.LONG, 5000)
    first.expected_net_loss = 5
    second = plan("BBBUSDT", Side.SHORT, 5000)
    second.expected_net_loss = 5

    broker.open(first, market)
    broker.open(second, market)

    assert broker.total_exposure == pytest.approx(10_000)
    assert broker.available_notional == pytest.approx(0)

    allowed, reason = broker.can_open("CCCUSDT")
    assert not allowed
    assert reason == "portfolio exposure budget exhausted"


def test_partial_releases_structural_risk_but_keeps_cost_reserve() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        taker_fee_rate=0.00055,
        slippage_bps=1,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        breakeven_buffer_bps=1,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("AAAUSDT", Side.LONG, 1000)
    p.expected_net_loss = 20
    broker.open(p, book(99.99, 100.00))

    before = broker.open_risk_usd
    assert broker.open_structural_risk_usd > 0
    assert broker.open_cost_reserve_usd > 0

    events = broker.mark(
        "AAAUSDT",
        100.60,
        book(100.60, 100.61),
    )
    assert events
    assert events[0]["event"] == "partial_take"

    assert broker.open_structural_risk_usd == pytest.approx(0)
    assert broker.open_cost_reserve_usd > 0
    assert broker.open_risk_usd == pytest.approx(
        broker.open_cost_reserve_usd
    )
    assert broker.open_risk_usd < before


def test_paper_fill_rejects_plan_that_exceeds_remaining_all_in_risk() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.02,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    broker = PaperBroker(cfg)
    p = plan("AAAUSDT", Side.LONG, 1000)
    p.expected_net_loss = 25

    with pytest.raises(
        RuntimeError,
        match="all-in portfolio risk",
    ):
        broker.open(p, book(99.99, 100.00))



def scalp_economic_plan(
    symbol: str,
    side: Side = Side.LONG,
) -> TradePlan:
    p = plan(symbol, side, 5000)
    p.leverage = 5.0
    p.stop = 99.90 if side == Side.LONG else 100.10
    p.target = 100.30 if side == Side.LONG else 99.70
    p.max_loss_usd = 5.0
    p.expected_net_loss = 11.5
    p.strategy_details = {
        "allowRunner": True,
        "economics": {
            "requiredNetProfitUsd": 1.0,
        },
    }
    return p


def test_tight_stop_partial_waits_until_closed_leg_is_net_profitable() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        taker_fee_rate=0.00055,
        slippage_bps=1,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        runner_target_r=2.5,
        breakeven_buffer_bps=1,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = scalp_economic_plan("AAAUSDT")
    broker.open(p, book(99.99, 100.00))

    # Roughly 1R in price terms, but the 70% leg would still be
    # negative after entry/exit fees and exit slippage.
    events = broker.mark(
        "AAAUSDT",
        100.12,
        book(100.12, 100.13),
    )
    assert events == []

    pos = broker.positions["AAAUSDT"]
    assert pos.mfe_r >= 1.0
    assert pos.partial_taken is False
    assert pos.partial_economic_ready is False
    assert pos.partial_net_preview_usd < 1.0
    assert pos.partial_required_net_usd == pytest.approx(1.0)

    # A little more continuation makes the partial economically useful.
    events = broker.mark(
        "AAAUSDT",
        100.16,
        book(100.16, 100.17),
    )
    assert events
    partial = events[0]
    assert partial["event"] == "partial_take"
    assert partial["netPnl"] >= 1.0
    assert partial["requiredNetUsd"] == pytest.approx(1.0)
    assert partial["economicReady"] is True

    pos = broker.positions["AAAUSDT"]
    assert pos.notional == pytest.approx(1500)
    assert pos.stop > pos.entry


def test_runner_stop_is_true_net_breakeven_after_costs() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        taker_fee_rate=0.00055,
        slippage_bps=1,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        runner_target_r=2.5,
        breakeven_buffer_bps=1,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = scalp_economic_plan("AAAUSDT")
    broker.open(p, book(99.99, 100.00))

    partial = broker.mark(
        "AAAUSDT",
        100.16,
        book(100.16, 100.17),
    )
    assert partial and partial[0]["event"] == "partial_take"
    locked = broker.total_pnl

    pos = broker.positions["AAAUSDT"]
    runner_stop = pos.stop
    assert runner_stop > pos.entry

    closed = broker.mark(
        "AAAUSDT",
        runner_stop,
        book(runner_stop, runner_stop + 0.01),
    )
    assert closed
    trade = closed[-1]
    assert trade["reason"] == "stop"
    assert trade["partialTaken"] is True

    # The remaining runner leg is protected after its own entry fee,
    # exit fee and exit slippage, so it cannot erase the locked partial.
    assert trade["netPnl"] >= locked - 0.01


def test_short_runner_breakeven_is_symmetric_after_partial() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        taker_fee_rate=0.00055,
        slippage_bps=1,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        runner_target_r=2.5,
        breakeven_buffer_bps=1,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = scalp_economic_plan("SHORTUSDT", Side.SHORT)
    broker.open(p, book(100.00, 100.01))

    partial = broker.mark(
        "SHORTUSDT",
        99.84,
        book(99.83, 99.84),
    )
    assert partial and partial[0]["event"] == "partial_take"
    locked = broker.total_pnl

    pos = broker.positions["SHORTUSDT"]
    runner_stop = pos.stop
    assert runner_stop < pos.entry

    closed = broker.mark(
        "SHORTUSDT",
        runner_stop,
        book(runner_stop - 0.01, runner_stop),
    )
    assert closed
    trade = closed[-1]
    assert trade["reason"] == "stop"
    assert trade["netPnl"] >= locked - 0.01


def test_open_uses_visible_depth_vwap() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        max_leverage=10,
        max_total_risk_fraction=1,
    )
    broker = PaperBroker(cfg)
    p = plan("DEPTHUSDT", Side.LONG, 200)
    layered = OrderBook(
        bids=[(99.90, 100)],
        asks=[(100.00, 1), (101.00, 2)],
    )

    pos = broker.open(p, layered)

    expected = 200 / (1 + 100 / 101)
    assert pos.entry == pytest.approx(expected)
    assert pos.entry > 100.00


def test_partial_keeps_structural_liquidity_target() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_at_r=1.0,
        partial_take_fraction=0.70,
        runner_target_r=2.5,
        breakeven_buffer_bps=0,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("LIQUSDT", Side.LONG, 1000)
    p.target = 100.80
    p.strategy_details = {
        "allowRunner": True,
        "targetSource": "liquidity",
        "liquidityTarget": {
            "price": 100.80,
            "kind": "resistance",
        },
    }
    broker.open(p, book(99.99, 100.00))

    events = broker.mark(
        "LIQUSDT",
        100.55,
        book(100.55, 100.56),
    )

    assert events and events[0]["event"] == "partial_take"
    pos = broker.positions["LIQUSDT"]
    assert pos.partial_taken is True
    assert pos.target == pytest.approx(100.80)
    assert pos.target < pos.entry + (
        abs(pos.entry - pos.initial_stop) * cfg.runner_target_r
    )



def test_target_exit_uses_resting_maker_limit_execution() -> None:
    cfg = Settings(
        taker_fee_rate=0.00055,
        maker_fee_rate=0.00020,
        slippage_bps=1,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_total_risk_fraction=1,
    )
    broker = PaperBroker(cfg)
    p = plan("TARGETMAKERUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.target = 101.0
    broker.open(p, book(99.99, 100.00))

    events = broker.mark(
        "TARGETMAKERUSDT",
        101.1,
        book(101.05, 101.06),
    )

    assert len(events) == 1
    trade = events[0]
    assert trade["reason"] == "target"
    assert trade["exit"] == pytest.approx(101.0)
    expected_entry_fee = 1000 * cfg.taker_fee_rate
    expected_exit_fee = (
        (1000 / 100.0)
        * 101.0
        * cfg.maker_fee_rate
    )
    assert trade["fees"] == pytest.approx(
        expected_entry_fee + expected_exit_fee
    )



def test_breakout_partial_is_resting_maker_and_uses_strategy_fraction() -> None:
    cfg = Settings(
        taker_fee_rate=0.00055,
        maker_fee_rate=0.00020,
        slippage_bps=1.0,
        maker_fill_confirmation_bps=0.5,
        partial_take_enabled=True,
        partial_take_at_r=1.0,
        breakout_partial_take_fraction=0.30,
        runner_target_r=2.5,
        no_follow_through_seconds=999,
        breakout_no_follow_through_seconds=999,
        max_total_risk_fraction=1,
    )
    broker = PaperBroker(cfg)
    p = plan("BREAKUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.strategy_details = {"allowRunner": True}
    pos = broker.open(p, book(99.99, 100.00))
    partial_limit = pos.entry + abs(pos.entry - pos.initial_stop)
    assert broker.mark(
        "BREAKUSDT",
        partial_limit,
        book(partial_limit, partial_limit + 0.01),
    ) == []
    crossed = partial_limit * (1 + cfg.maker_fill_confirmation_bps / 10_000)
    events = broker.mark(
        "BREAKUSDT",
        crossed,
        book(crossed, crossed + 0.01),
    )
    assert events and events[0]["event"] == "partial_take"
    partial = events[0]
    assert partial["closedNotional"] == pytest.approx(300)
    assert partial["remainingNotional"] == pytest.approx(700)
    assert partial["fill"] == pytest.approx(partial_limit)
    expected_allocated_entry_fee = 1000 * cfg.taker_fee_rate * 0.30
    expected_maker_exit_fee = (
        (1000 / 100.0)
        * 0.30
        * partial_limit
        * cfg.maker_fee_rate
    )
    assert partial["fees"] == pytest.approx(
        expected_allocated_entry_fee + expected_maker_exit_fee
    )


def test_breakout_no_follow_through_waits_longer_than_density() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_max_mfe_r=0.25,
        early_cut_at_r=0.45,
        breakout_no_follow_through_seconds=120,
        density_no_follow_through_seconds=20,
    )
    breakout = PaperBroker(cfg)
    breakout_plan = plan("BREAKUSDT", Side.LONG, 1000)
    breakout_plan.strategy = "level_breakout"
    breakout.open(breakout_plan, book(99.99, 100.00))
    breakout.positions["BREAKUSDT"].opened_at -= 30
    assert breakout.mark(
        "BREAKUSDT", 99.75, book(99.75, 99.76)
    ) == []
    assert "BREAKUSDT" in breakout.positions

    density = PaperBroker(cfg)
    density_plan = plan("DENSUSDT", Side.LONG, 1000)
    density_plan.strategy = "orderbook_density"
    density.open(density_plan, book(99.99, 100.00))
    density.positions["DENSUSDT"].opened_at -= 30
    events = density.mark(
        "DENSUSDT", 99.75, book(99.75, 99.76)
    )
    assert events and events[-1]["reason"] == "no_follow_through"



def test_pending_maker_entry_reserves_budget_and_requires_trade_through() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        maker_fee_rate=0.00020,
        taker_fee_rate=0.00055,
        passive_entry_enabled=True,
        passive_entry_timeout_seconds=15,
        maker_fill_confirmation_bps=0.5,
    )
    broker = PaperBroker(cfg)
    p = plan("PASSIVEUSDT", Side.LONG, 1000)
    p.strategy = "orderbook_density"
    p.entry_mode = "maker_limit"
    p.market_entry = 99.99
    p.expected_net_loss = 10
    pending = broker.place_pending(p)
    assert pending.limit_price == pytest.approx(99.99)
    assert broker.pending_exposure_usd == pytest.approx(1000)
    assert broker.pending_risk_usd == pytest.approx(10)
    assert broker.available_notional == pytest.approx(9000)
    assert broker.mark_pending("PASSIVEUSDT", 99.99) == []
    assert "PASSIVEUSDT" in broker.pending_entries
    assert "PASSIVEUSDT" not in broker.positions
    through = 99.99 * (1 - cfg.maker_fill_confirmation_bps / 10_000)
    events = broker.mark_pending("PASSIVEUSDT", through)
    assert events and events[0]["event"] == "entry_filled"
    assert "PASSIVEUSDT" not in broker.pending_entries
    pos = broker.positions["PASSIVEUSDT"]
    assert pos.entry == pytest.approx(99.99)
    assert pos.entry_fee_remaining == pytest.approx(
        p.notional * cfg.maker_fee_rate
    )


def test_pending_maker_entry_timeout_releases_reservation() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        passive_entry_enabled=True,
        passive_entry_timeout_seconds=15,
    )
    broker = PaperBroker(cfg)
    p = plan("TIMEOUTUSDT", Side.LONG, 1000)
    p.entry_mode = "maker_limit"
    p.market_entry = 99.99
    p.expected_net_loss = 10
    broker.place_pending(p)
    broker.pending_entries["TIMEOUTUSDT"].expires_at -= 60
    events = broker.mark_pending("TIMEOUTUSDT", 100.0)
    assert events and events[0]["event"] == "entry_cancelled"
    assert events[0]["reason"] == "passive_entry_timeout"
    assert broker.pending_exposure_usd == 0
    assert broker.pending_risk_usd == 0


def test_pending_maker_entry_ignores_trade_from_before_order() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        passive_entry_enabled=True,
        passive_entry_timeout_seconds=15,
        maker_fill_confirmation_bps=0.5,
    )
    broker = PaperBroker(cfg)
    p = plan("STALEUSDT", Side.LONG, 1000)
    p.entry_mode = "maker_limit"
    p.market_entry = 99.99
    p.expected_net_loss = 10
    pending = broker.place_pending(
        p,
        min_trade_ts_ms=1_000,
    )
    through = 99.99 * (1 - cfg.maker_fill_confirmation_bps / 10_000)
    events = broker.mark_pending(
        "STALEUSDT",
        through,
        trade_ts_ms=1_000,
    )
    assert events == []
    assert "STALEUSDT" in broker.pending_entries
    assert "STALEUSDT" not in broker.positions


def test_expire_pending_releases_reservation_without_trade() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        passive_entry_enabled=True,
        passive_entry_timeout_seconds=15,
    )
    broker = PaperBroker(cfg)
    p = plan("EXPIREUSDT", Side.LONG, 1000)
    p.entry_mode = "maker_limit"
    p.market_entry = 99.99
    p.expected_net_loss = 10
    pending = broker.place_pending(p)
    events = broker.expire_pending(pending.expires_at + 0.01)
    assert events and events[0]["reason"] == "passive_entry_timeout"
    assert broker.pending_exposure_usd == 0
    assert broker.pending_risk_usd == 0



def test_position_public_tracks_price_movement_timing_and_fee_estimate() -> None:
    cfg = Settings(
        taker_fee_rate=0.0005,
        maker_fee_rate=0.0002,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    pos = broker.open(
        plan("TRACKUSDT", Side.LONG, 1000),
        book(99.99, 100.00),
    )
    assert pos.entry_fee_total_usd == pytest.approx(0.5)

    broker.mark(
        "TRACKUSDT",
        100.30,
        book(100.30, 100.31),
    )
    public = broker.positions["TRACKUSDT"].public()

    assert public["current_move_pct"] > 0
    assert public["max_favorable_move_pct"] > 0
    assert public["mfe_at"] is not None
    assert public["mfe_price"] is not None
    assert public["fees_committed_usd"] == pytest.approx(0.5)
    assert public["estimated_total_fees_if_close_now_usd"] > 0.5


def test_closed_trade_persists_move_extremes_timestamps_and_fees() -> None:
    cfg = Settings(
        taker_fee_rate=0.0005,
        maker_fee_rate=0.0002,
        slippage_bps=0,
        maker_fill_confirmation_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("CLOSETRACKUSDT", Side.LONG, 1000)
    p.strategy_details = {
        "economics": {
            "firstTakeMovePct": 0.003,
            "movementFloorBands": {
                "0.10%": True,
                "0.15%": True,
                "0.20%": True,
                "0.25%": True,
                "0.30%": True,
            },
        }
    }
    broker.open(p, book(99.99, 100.00))
    broker.mark(
        "CLOSETRACKUSDT",
        99.80,
        book(99.80, 99.81),
    )
    events = broker.mark(
        "CLOSETRACKUSDT",
        101.00,
        book(101.00, 101.01),
    )

    trade = events[-1]
    assert trade["event"] == "trade_closed"
    assert trade["exitMoveBps"] > 0
    assert trade["maxFavorableMoveBps"] > 0
    assert trade["maxAdverseMoveBps"] > 0
    assert trade["mfeAt"] is not None
    assert trade["maeAt"] is not None
    assert trade["fees"] > 0
    assert trade["entryFeeUsd"] == pytest.approx(0.5)
    assert trade["plannedFirstTakeMovePct"] == pytest.approx(0.003)
    assert trade["movementFloorBands"]["0.30%"] is True



def test_staged_add_aggregates_position_without_widening_stop() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.10,
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    market = book(99.99, 100.00)

    probe = plan("STAGEDUSDT", Side.LONG, 300)
    probe.strategy = "level_breakout"
    probe.setup_id = "level_breakout:long:g1"
    probe.stop = 99.5
    probe.expected_net_loss = 2.0
    probe.strategy_details = {
        "stagedEntry": {
            "phase": "probe",
            "riskFraction": 0.35,
        }
    }
    first = broker.open(probe, market)
    first_entry = first.entry
    first_risk = first.initial_risk_usd

    add = plan("STAGEDUSDT", Side.LONG, 400)
    add.strategy = "level_breakout"
    add.setup_id = probe.setup_id
    add.stop = 99.6
    add.target = 101.20
    add.expected_net_loss = 2.0
    add.strategy_details = {
        "stagedEntry": {
            "phase": "add",
            "riskFraction": 0.65,
        }
    }
    position = broker.add(
        add,
        book(100.19, 100.20),
    )

    assert position.notional == pytest.approx(700)
    assert position.original_notional == pytest.approx(700)
    assert position.entry > first_entry
    assert position.stop == pytest.approx(99.6)
    assert position.target == pytest.approx(101.20)
    assert position.strategy_details[
        "stagedPositionGeometry"
    ]["target"] == pytest.approx(101.20)
    assert position.strategy_details[
        "stagedPositionEconomics"
    ]["netRewardRisk"] >= 1.0
    assert len(position.entry_legs) == 2
    assert position.entry_legs[0]["phase"] == "probe"
    assert position.entry_legs[1]["phase"] == "add"
    assert position.initial_risk_usd > first_risk
    assert broker.total_exposure == pytest.approx(700)

    closed = broker.close(
        "STAGEDUSDT",
        book(100.40, 100.41),
        "test",
    )
    assert closed["scaleInCount"] == 1
    assert len(closed["entryLegs"]) == 2


def test_staged_add_rejects_wrong_setup_and_side() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.10,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    broker = PaperBroker(cfg)
    probe = plan("STAGEDUSDT", Side.LONG, 300)
    probe.strategy = "weak_level_rejection"
    probe.setup_id = "weak:one"
    broker.open(probe, book(99.99, 100.00))

    wrong_setup = plan("STAGEDUSDT", Side.LONG, 100)
    wrong_setup.strategy = probe.strategy
    wrong_setup.setup_id = "weak:two"
    allowed, reason = broker.can_add(wrong_setup)
    assert not allowed
    assert "setup" in reason

    wrong_side = plan("STAGEDUSDT", Side.SHORT, 100)
    wrong_side.strategy = probe.strategy
    wrong_side.setup_id = probe.setup_id
    allowed, reason = broker.can_add(wrong_side)
    assert not allowed
    assert "side" in reason


def test_pending_maker_add_fills_into_existing_position() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.10,
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        passive_entry_enabled=True,
        maker_fill_confirmation_bps=0,
    )
    broker = PaperBroker(cfg)

    probe = plan("STAGEDUSDT", Side.LONG, 300)
    probe.strategy = "weak_level_rejection"
    probe.setup_id = "weak:maker"
    probe.strategy_details = {
        "stagedEntry": {"phase": "probe", "riskFraction": 0.30}
    }
    broker.open(probe, book(99.99, 100.00))

    add = plan("STAGEDUSDT", Side.LONG, 200)
    add.strategy = "weak_level_rejection"
    add.setup_id = probe.setup_id
    add.entry_mode = "maker_limit"
    add.market_entry = 99.99
    add.strategy_details = {
        "stagedEntry": {"phase": "add", "riskFraction": 0.70}
    }

    pending = broker.place_pending_add(add)
    assert pending.position_action == "add"
    events = broker.mark_pending(
        "STAGEDUSDT",
        99.98,
        trade_ts_ms=int(pending.created_at * 1000) + 1,
    )
    assert events
    assert events[0]["event"] == "entry_added"
    assert broker.positions["STAGEDUSDT"].notional == pytest.approx(500)
    assert len(broker.positions["STAGEDUSDT"].entry_legs) == 2



def test_staged_add_rejects_bad_aggregate_payoff_even_when_leg_is_valid() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.10,
        absolute_min_net_reward_risk=1.0,
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)

    probe = plan("BADADDUSDT", Side.LONG, 300)
    probe.strategy = "weak_level_rejection"
    probe.setup_id = "weak:bad-add"
    probe.stop = 99.50
    probe.target = 101.50
    broker.open(probe, book(99.99, 100.00))

    add = plan("BADADDUSDT", Side.LONG, 400)
    add.strategy = probe.strategy
    add.setup_id = probe.setup_id
    add.stop = 99.60
    # This add target would make the combined weighted position expect
    # less upside than downside.
    add.target = 100.30
    add.strategy_details = {
        "stagedEntry": {"phase": "add", "riskFraction": 0.70}
    }

    with pytest.raises(
        RuntimeError,
        match="combined staged position net reward/risk",
    ):
        broker.add(
            add,
            book(100.19, 100.20),
        )



def test_maker_partial_does_not_fill_on_book_only_touch() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0.5,
        partial_take_enabled=True,
        partial_take_at_r=1.0,
        partial_take_fraction=0.5,
        min_net_profit_usd=0,
        min_net_profit_equity_fraction=0,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("MAKERPARTUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    broker.open(p, book(99.99, 100.00))

    partial_limit = broker._partial_limit_price(
        broker.positions["MAKERPARTUSDT"]
    )
    events = broker.mark(
        "MAKERPARTUSDT",
        partial_limit,
        book(partial_limit + 0.01, partial_limit + 0.02),
        trade_price=None,
    )
    assert events == []
    assert broker.positions["MAKERPARTUSDT"].partial_taken is False

    events = broker.mark(
        "MAKERPARTUSDT",
        partial_limit * 1.0001,
        book(partial_limit, partial_limit + 0.01),
        trade_price=partial_limit * 1.0001,
    )
    assert events
    assert events[0]["event"] == "partial_take"



def test_paper_does_not_take_partial_when_plan_marks_it_unprofitable() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0,
        partial_take_enabled=True,
        partial_take_at_r=1.0,
        breakout_partial_take_fraction=0.30,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("NOPARTUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.strategy_details = {
        "allowRunner": True,
        "economics": {
            "partialPlanned": False,
            "partialRequiredNetUsd": 1.0,
        },
    }
    broker.open(p, book(99.99, 100.00))

    events = broker.mark(
        "NOPARTUSDT",
        100.60,
        book(100.60, 100.61),
        trade_price=100.60,
    )

    assert not any(
        event["event"] == "partial_take"
        for event in events
    )
    assert broker.positions["NOPARTUSDT"].partial_taken is False


def test_pending_maker_entry_ignores_wrong_aggressor_side() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        maker_fee_rate=0,
        taker_fee_rate=0,
        passive_entry_enabled=True,
        maker_fill_confirmation_bps=0,
        maker_queue_ahead_fraction=0.0,
    )
    broker = PaperBroker(cfg)
    p = plan("SIDEENTRYUSDT", Side.LONG, 1000)
    p.strategy = "orderbook_density"
    p.entry_mode = "maker_limit"
    p.market_entry = 99.99
    p.expected_net_loss = 10
    pending = broker.place_pending(p)

    assert broker.mark_pending(
        "SIDEENTRYUSDT",
        99.98,
        trade_notional_usd=2000,
        trade_side="Buy",
    ) == []
    assert pending.eligible_trade_notional_usd == 0
    assert "SIDEENTRYUSDT" in broker.pending_entries

    filled = broker.mark_pending(
        "SIDEENTRYUSDT",
        99.98,
        trade_notional_usd=2000,
        trade_side="Sell",
    )
    assert filled and filled[0]["event"] == "entry_filled"


def test_maker_exit_ignores_wrong_aggressor_side() -> None:
    cfg = Settings(
        maker_fee_rate=0,
        taker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0,
        maker_queue_ahead_fraction=0.0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("SIDEEXITUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.target = 101.0
    broker.open(p, book(99.99, 100.00))

    assert broker.mark(
        "SIDEEXITUSDT",
        101.0,
        book(101.0, 101.01),
        trade_price=101.0,
        trade_notional_usd=2000,
        trade_side="Sell",
    ) == []
    assert "SIDEEXITUSDT" in broker.positions

    closed = broker.mark(
        "SIDEEXITUSDT",
        101.0,
        book(101.0, 101.01),
        trade_price=101.0,
        trade_notional_usd=2000,
        trade_side="Buy",
    )
    assert closed and closed[-1]["reason"] == "target"


def test_pending_maker_entry_requires_cumulative_trade_through_volume() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.05,
        maker_fee_rate=0,
        taker_fee_rate=0,
        passive_entry_enabled=True,
        maker_fill_confirmation_bps=0,
        maker_queue_ahead_fraction=0.50,
    )
    broker = PaperBroker(cfg)
    p = plan("VOLENTRYUSDT", Side.LONG, 1000)
    p.strategy = "orderbook_density"
    p.entry_mode = "maker_limit"
    p.market_entry = 99.99
    p.expected_net_loss = 10
    pending = broker.place_pending(p)

    first = broker.mark_pending(
        "VOLENTRYUSDT",
        99.98,
        trade_notional_usd=100,
    )
    assert first == []
    assert "VOLENTRYUSDT" in broker.pending_entries
    assert pending.eligible_trade_notional_usd == pytest.approx(100)
    assert pending.required_trade_notional_usd == pytest.approx(1500)

    filled = broker.mark_pending(
        "VOLENTRYUSDT",
        99.98,
        trade_notional_usd=1400,
    )
    assert filled and filled[0]["event"] == "entry_filled"
    assert filled[0]["fillModel"] == "trade_through_volume"


def test_maker_target_requires_cumulative_trade_through_volume() -> None:
    cfg = Settings(
        maker_fee_rate=0,
        taker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0,
        maker_queue_ahead_fraction=0.50,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("VOLTARGETUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.target = 101.0
    broker.open(p, book(99.99, 100.00))

    first = broker.mark(
        "VOLTARGETUSDT",
        101.0,
        book(101.0, 101.01),
        trade_price=101.0,
        trade_notional_usd=100,
    )
    assert first == []
    assert "VOLTARGETUSDT" in broker.positions

    closed = broker.mark(
        "VOLTARGETUSDT",
        101.0,
        book(101.0, 101.01),
        trade_price=101.0,
        trade_notional_usd=1415,
    )
    assert closed and closed[-1]["reason"] == "target"


def test_maker_partial_requires_cumulative_trade_through_volume() -> None:
    cfg = Settings(
        maker_fee_rate=0,
        taker_fee_rate=0,
        slippage_bps=0,
        maker_fill_confirmation_bps=0,
        maker_queue_ahead_fraction=0.50,
        partial_take_enabled=True,
        partial_take_at_r=1.0,
        breakout_partial_take_fraction=0.30,
        runner_target_r=2.5,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("VOLPARTUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.target = 102.0
    broker.open(p, book(99.99, 100.00))
    pos = broker.positions["VOLPARTUSDT"]
    partial_limit = broker._partial_limit_price(pos)

    first = broker.mark(
        "VOLPARTUSDT",
        partial_limit,
        book(partial_limit, partial_limit + 0.01),
        trade_price=partial_limit,
        trade_notional_usd=50,
    )
    assert first == []
    assert pos.partial_taken is False

    partial = broker.mark(
        "VOLPARTUSDT",
        partial_limit,
        book(partial_limit, partial_limit + 0.01),
        trade_price=partial_limit,
        trade_notional_usd=403,
    )
    assert partial and partial[0]["event"] == "partial_take"
    assert pos.partial_taken is True


def test_market_exit_penalizes_unseen_depth_tail() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        paper_missing_depth_penalty_bps=25.0,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("TAILUSDT", Side.LONG, 1000)
    p.stop = 99.5
    p.target = 102.0
    broker.open(
        p,
        OrderBook(
            bids=[(99.99, 100)],
            asks=[(100.00, 100)],
        ),
    )

    shallow = OrderBook(
        bids=[(99.00, 1.0)],
        asks=[(99.10, 1.0)],
    )
    trade = broker.close(
        "TAILUSDT",
        shallow,
        "manual_test",
    )

    visible_quantity = 1.0
    total_quantity = 10.0
    missing_quantity = total_quantity - visible_quantity
    missing_fraction = missing_quantity / total_quantity
    tail_price = 99.0 * (
        1 - (25.0 / 10_000) * missing_fraction
    )
    expected = (
        99.0 * visible_quantity
        + tail_price * missing_quantity
    ) / total_quantity
    assert trade["exit"] == pytest.approx(expected)
    assert trade["exit"] < 99.0


def test_fast_book_triggers_stop_while_deep_book_sets_exit_vwap() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("FASTSTOPUSDT", Side.LONG, 1000)
    p.stop = 99.50
    p.target = 102.0
    broker.open(
        p,
        OrderBook(
            bids=[(99.99, 100)],
            asks=[(100.00, 100)],
        ),
    )

    fast = OrderBook(
        bids=[(99.40, 100)],
        asks=[(99.41, 100)],
    )
    deep = OrderBook(
        bids=[(99.00, 100)],
        asks=[(99.10, 100)],
    )
    events = broker.mark(
        "FASTSTOPUSDT",
        99.40,
        fast,
        depth_book=deep,
    )

    assert events and events[-1]["reason"] == "stop"
    assert events[-1]["exit"] == pytest.approx(99.00)


def test_explicit_contract_quantity_controls_scale_in_average_entry() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=10,
        max_total_risk_fraction=0.20,
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)

    first = plan("QTYUSDT", Side.LONG, 100)
    first.strategy = "level_breakout"
    first.setup_id = "qty:g1"
    first.quantity = 1.0
    first.market_entry = 100.0
    broker.open(
        first,
        OrderBook(
            bids=[(99.9, 10.0)],
            asks=[(100.0, 10.0)],
        ),
    )

    add = plan("QTYUSDT", Side.LONG, 200)
    add.strategy = "level_breakout"
    add.setup_id = first.setup_id
    add.quantity = 1.0
    add.market_entry = 200.0
    add.stop = 99.6
    add.target = 301.0
    add.expected_net_loss = 5.0
    position = broker.add(
        add,
        OrderBook(
            bids=[(199.9, 10.0)],
            asks=[(200.0, 10.0)],
        ),
    )

    assert position.quantity == pytest.approx(2.0)
    assert position.original_quantity == pytest.approx(2.0)
    assert position.entry == pytest.approx(150.0)
    assert position.notional == pytest.approx(300.0)
    assert position.original_notional == pytest.approx(300.0)


def test_exit_fee_uses_filled_quantity_times_exit_price() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        maker_fee_rate=0.001,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
        max_leverage=2,
    )
    broker = PaperBroker(cfg)
    p = plan("FEEQTYUSDT", Side.LONG, 1000)
    p.strategy = "level_breakout"
    p.quantity = 10.0
    p.target = 110.0
    broker.open(
        p,
        book(99.99, 100.0),
    )

    events = broker.mark(
        "FEEQTYUSDT",
        110.0,
        book(110.0, 110.01),
        trade_price=110.0,
        trade_notional_usd=5000,
        trade_side="Buy",
    )
    trade = events[-1]

    assert trade["reason"] == "target"
    assert trade["fees"] == pytest.approx(
        10.0 * 110.0 * cfg.maker_fee_rate
    )
