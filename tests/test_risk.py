from scalp_bot.config import Settings
from scalp_bot.domain import Action, StrategyDecision
from scalp_bot.risk import RiskEngine


def decision(target: float) -> StrategyDecision:
    return StrategyDecision(
        strategy="test",
        action=Action.LONG,
        reasons=["test"],
        entry=100.0,
        stop=99.8,
        target=target,
    )


def test_rejects_prediction_smaller_than_costs_and_minimum_profit() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        min_net_profit_usd=1,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(100.05), 1000, spread_pct=0.0001)
    assert not result.allowed
    assert "expected net" in result.reason


def test_accepts_trade_with_real_net_edge() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        min_net_profit_usd=1,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(100.5), 1000, spread_pct=0.0001)
    assert result.allowed
    assert result.plan is not None
    assert result.plan.expected_net_profit > 1
