from scalp_bot.domain import TradeTick
from scalp_bot.strategy.common import compute_trade_flow


def trade(ts_ms: int, price: float, side: str, size: float = 1.0) -> TradeTick:
    return TradeTick(
        ts_ms=ts_ms,
        price=price,
        size=size,
        side=side,
    )


def test_trade_flow_exposes_price_response_for_directional_effort() -> None:
    now = 20_000
    trades = [
        trade(1_000, 100.00, "Buy"),
        trade(3_000, 100.01, "Buy"),
        trade(5_000, 100.02, "Sell"),
        trade(7_000, 100.02, "Buy"),
        trade(16_000, 100.03, "Buy", 2.0),
        trade(17_000, 100.06, "Buy", 2.0),
        trade(18_000, 100.09, "Buy", 2.0),
        trade(19_000, 100.12, "Buy", 2.0),
        trade(20_000, 100.15, "Buy", 2.0),
    ]

    flow = compute_trade_flow(trades, now)

    assert flow["priceMove5sPct"] > 0
    assert flow["imbalance5s"] > 0
    assert flow["priceResponseEfficiency5s"] > 0
    assert flow["effortWithoutResult5s"] is False


def test_trade_flow_flags_effort_without_price_result() -> None:
    now = 20_000
    trades = [
        trade(1_000, 100.00, "Sell"),
        trade(3_000, 100.01, "Buy"),
        trade(5_000, 100.00, "Sell"),
        trade(7_000, 100.01, "Buy"),
        trade(16_000, 100.0000, "Buy", 3.0),
        trade(17_000, 100.0001, "Buy", 3.0),
        trade(18_000, 100.0000, "Buy", 3.0),
        trade(19_000, 100.0001, "Buy", 3.0),
        trade(20_000, 100.0000, "Buy", 3.0),
    ]

    flow = compute_trade_flow(trades, now)

    assert flow["imbalance5s"] > 0.9
    assert abs(flow["priceMove5sPct"]) < 0.00003
    assert flow["acceleration"] >= 1.0
    assert flow["effortWithoutResult5s"] is True
