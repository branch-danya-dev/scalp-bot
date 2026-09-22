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



def test_target_uses_executable_exit_not_last_trade() -> None:
    cfg = Settings(
        taker_fee_rate=0,
        slippage_bps=0,
        partial_take_enabled=False,
        no_follow_through_seconds=999,
    )
    broker = PaperBroker(cfg)
    p = plan("AAAUSDT", Side.LONG, 100)
    p.target = 101.0
    broker.open(p, book(99.99, 100.00))

    # Last trade printed through target, but executable bid did not.
    events = broker.mark("AAAUSDT", 101.10, book(100.95, 101.05))
    assert events == []
    assert "AAAUSDT" in broker.positions

    events = broker.mark("AAAUSDT", 101.10, book(101.00, 101.05))
    assert events and events[-1]["reason"] == "target"


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
