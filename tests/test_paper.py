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
        entry_drift_pct=0,
    )


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook(bids=[(bid, 100)], asks=[(ask, 100)])


def test_position_can_go_negative_without_being_closed_before_stop() -> None:
    cfg = Settings(taker_fee_rate=0, slippage_bps=0, max_leverage=1, max_open_positions=4)
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG), book(99.99, 100.00))
    closed = broker.mark("AAAUSDT", 99.70, book(99.69, 99.70))
    assert closed is None
    assert "AAAUSDT" in broker.positions
    assert broker.positions["AAAUSDT"].mae_usd > 0

    closed = broker.mark("AAAUSDT", 101.00, book(101.00, 101.01))
    assert closed is not None
    assert closed["reason"] == "target"
    assert closed["maeUsd"] > 0


def test_positions_are_isolated_by_symbol_but_share_exposure_budget() -> None:
    cfg = Settings(taker_fee_rate=0, slippage_bps=0, max_leverage=1, max_open_positions=4)
    broker = PaperBroker(cfg)
    broker.open(plan("AAAUSDT", Side.LONG, 400), book(99.99, 100.00))
    broker.open(plan("BBBUSDT", Side.SHORT, 400), book(100.00, 100.01))
    assert set(broker.positions) == {"AAAUSDT", "BBBUSDT"}
    assert broker.total_exposure == 800
    assert broker.available_notional == 200
    assert broker.mark("AAAUSDT", 99.8, book(99.79, 99.80)) is None
    assert broker.positions["BBBUSDT"].mae_usd == 0
