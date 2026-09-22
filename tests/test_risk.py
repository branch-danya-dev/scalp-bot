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
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        min_net_profit_usd=1,
        min_net_reward_risk=0,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(100.05), 1000, book(99.99, 100.01), 1000, 20)
    assert not result.allowed
    assert "net at target" in result.reason or "target" in result.reason


def test_accepts_trade_with_real_net_edge() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        min_net_profit_usd=1,
        min_net_reward_risk=1.0,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(100.6), 1000, book(99.99, 100.01), 1000, 20)
    assert result.allowed
    assert result.plan is not None
    assert result.plan.expected_net_profit > 1
    assert result.plan.net_reward_risk >= 1.0


def test_rejects_chasing_price_after_setup_has_moved() -> None:
    cfg = Settings(max_entry_drift_bps=5, min_net_profit_usd=0, min_net_reward_risk=0)
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(102, stop=99), 1000, book(100.09, 100.10), 1000, 20)
    assert not result.allowed
    assert "expired" in result.reason


def test_better_long_entry_below_setup_is_allowed_while_stop_is_intact() -> None:
    cfg = Settings(
        max_entry_drift_bps=5,
        min_net_profit_usd=0,
        min_net_reward_risk=0,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    result = RiskEngine(cfg).build_plan("BTCUSDT", decision(102, stop=99), 1000, book(99.89, 99.90), 1000, 20)
    assert result.allowed


def test_rejects_trade_with_bad_net_reward_risk_even_if_profit_covers_costs() -> None:
    cfg = Settings(
        min_net_profit_usd=0.1,
        min_net_reward_risk=1.5,
        enforce_net_reward_risk_gate=True,
        taker_fee_rate=0,
        slippage_bps=0,
        max_leverage=1,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.30, stop=99.70),
        1000,
        book(99.99, 100.00),
        1000,
        20,
    )
    assert not result.allowed
    assert "reward/risk" in result.reason



def test_single_trade_cannot_consume_whole_portfolio_exposure() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        max_open_positions=4,
        max_position_exposure_fraction=0.25,
        risk_fraction=0.005,
        min_net_profit_usd=0,
        min_net_reward_risk=0,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(101.0, stop=99.9),
        1000,
        book(99.99, 100.00),
        1000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == 250


def test_position_exposure_cap_never_exceeds_remaining_portfolio_exposure() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        max_position_exposure_fraction=0.25,
        risk_fraction=0.005,
        min_net_profit_usd=0,
        min_net_reward_risk=0,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(101.0, stop=99.9),
        1000,
        book(99.99, 100.00),
        80,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == 80



def test_research_mode_allows_positive_net_setup_even_when_net_rr_is_below_live_gate() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        max_position_exposure_fraction=0.25,
        risk_fraction=0.005,
        min_net_profit_usd=0.10,
        min_net_reward_risk=1.15,
        enforce_net_reward_risk_gate=False,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.35, stop=99.80),
        1000,
        book(99.99, 100.00),
        1000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.expected_net_profit >= 0.10
    assert result.plan.net_reward_risk < 1.15


def test_research_mode_still_rejects_setup_with_negative_expected_net_after_costs() -> None:
    cfg = Settings(
        start_balance=1000,
        max_leverage=1,
        max_position_exposure_fraction=0.25,
        min_net_profit_usd=0,
        enforce_net_reward_risk_gate=False,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.10, stop=99.90),
        1000,
        book(99.99, 100.00),
        1000,
        20,
    )

    assert not result.allowed
    assert "after estimated trading costs" in result.reason
