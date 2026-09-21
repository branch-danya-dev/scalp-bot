from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.risk import RiskEngine


def decision(target: float, *, entry: float = 100.0, stop: float = 99.8) -> StrategyDecision:
    return StrategyDecision(
        strategy="test",
        action=Action.LONG,
        reasons=["test"],
        entry=entry,
        stop=stop,
        target=target,
    )


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook(bids=[(bid, 10)], asks=[(ask, 10)])


def test_rejects_prediction_smaller_than_costs_and_minimum_profit() -> None:
    cfg = Settings(start_balance=1000, max_leverage=1, min_net_profit_usd=1, taker_fee_rate=0.00055, slippage_bps=1)
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(100.05), 1000, book(99.99, 100.01), 1000, 20)
    assert not result.allowed
    assert "expected net" in result.reason or "target" in result.reason


def test_accepts_trade_with_real_net_edge() -> None:
    cfg = Settings(start_balance=1000, max_leverage=1, min_net_profit_usd=1, taker_fee_rate=0.00055, slippage_bps=1)
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(100.5), 1000, book(99.99, 100.01), 1000, 20)
    assert result.allowed
    assert result.plan is not None
    assert result.plan.expected_net_profit > 1


def test_rejects_chasing_price_after_setup_has_moved() -> None:
    cfg = Settings(max_entry_drift_bps=5, min_net_profit_usd=0)
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(102, stop=99), 1000, book(100.09, 100.10), 1000, 20)
    assert not result.allowed
    assert "expired" in result.reason


def test_better_long_entry_below_setup_is_allowed_while_stop_is_intact() -> None:
    cfg = Settings(max_entry_drift_bps=5, min_net_profit_usd=0, taker_fee_rate=0, slippage_bps=0)
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(102, stop=99), 1000, book(99.89, 99.90), 1000, 20)
    assert result.allowed
