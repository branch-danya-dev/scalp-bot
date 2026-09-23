import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action, OrderBook, StrategyDecision
from scalp_bot.paper import PaperBroker
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
    return OrderBook(bids=[(bid, 100)], asks=[(ask, 100)])


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
        min_net_profit_equity_fraction=0.0,
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
        min_net_profit_equity_fraction=0.0,
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
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        taker_fee_rate=0.00055,
        slippage_bps=1,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.08, stop=99.90),
        1000,
        book(99.99, 100.00),
        1000,
        20,
    )

    assert not result.allowed
    assert "after estimated trading costs" in result.reason



def scalp_settings(**overrides) -> Settings:
    values = dict(
        start_balance=1000,
        risk_fraction=0.005,
        max_trade_all_in_loss_fraction=0.0125,
        max_total_risk_fraction=0.02,
        max_leverage=10.0,
        max_position_leverage=5.0,
        max_position_exposure_fraction=1.0,
        min_net_profit_usd=0,
        min_net_profit_equity_fraction=0.0,
        min_net_reward_risk=0,
        enforce_net_reward_risk_gate=False,
        taker_fee_rate=0,
        slippage_bps=0,
    )
    values.update(overrides)
    return Settings(**values)


def test_controlled_risk_scale_increases_structural_budget_but_keeps_hard_cap() -> None:
    trade = decision(100.60, stop=99.80)
    trade.details["riskScale"] = 1.20
    trade.details["riskScaleSource"] = "test"

    result = RiskEngine(scalp_settings()).build_plan(
        "BTCUSDT",
        trade,
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(3000)
    economics = result.plan.strategy_details["economics"]
    assert economics["baseStructuralRiskBudgetUsd"] == pytest.approx(5.0)
    assert economics["structuralRiskBudgetUsd"] == pytest.approx(6.0)
    assert economics["riskScale"] == pytest.approx(1.20)
    hard_cap = 1000 * scalp_settings().max_trade_all_in_loss_fraction
    assert result.plan.expected_net_loss <= hard_cap + 1e-9


def test_tight_stop_scales_position_to_five_x() -> None:
    result = RiskEngine(scalp_settings()).build_plan(
        "BTCUSDT",
        decision(100.5, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(5000)
    assert result.plan.leverage == pytest.approx(5.0)
    assert result.plan.max_loss_usd == pytest.approx(5.0)


def test_two_tenths_percent_stop_sizes_to_two_and_half_x() -> None:
    result = RiskEngine(scalp_settings()).build_plan(
        "BTCUSDT",
        decision(100.6, stop=99.80),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(2500)
    assert result.plan.leverage == pytest.approx(2.5)
    assert result.plan.max_loss_usd == 5.0


def test_wider_stop_naturally_reduces_effective_leverage() -> None:
    result = RiskEngine(scalp_settings()).build_plan(
        "BTCUSDT",
        decision(101.0, stop=99.50),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(1000)
    assert result.plan.leverage == pytest.approx(1.0)
    assert result.plan.max_loss_usd == 5.0


def test_position_leverage_cap_is_separate_from_portfolio_cap() -> None:
    result = RiskEngine(
        scalp_settings(
            max_leverage=10.0,
            max_position_leverage=3.0,
        )
    ).build_plan(
        "BTCUSDT",
        decision(100.5, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(3000)
    assert result.plan.leverage == pytest.approx(3.0)


def test_remaining_portfolio_notional_still_caps_tight_stop_trade() -> None:
    result = RiskEngine(scalp_settings()).build_plan(
        "BTCUSDT",
        decision(100.5, stop=99.90),
        1000,
        book(99.99, 100.00),
        1800,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(1800)
    assert result.plan.leverage == pytest.approx(1.8)



def economic_settings(**overrides) -> Settings:
    values = dict(
        start_balance=1000,
        risk_fraction=0.005,
        max_total_risk_fraction=0.02,
        max_leverage=10.0,
        max_position_leverage=5.0,
        max_position_exposure_fraction=1.0,
        min_net_profit_usd=1.0,
        min_net_profit_equity_fraction=0.001,
        enforce_min_net_profit_gate=True,
        min_net_reward_risk=1.15,
        enforce_net_reward_risk_gate=True,
        taker_fee_rate=0.00055,
        slippage_bps=1.0,
    )
    values.update(overrides)
    return Settings(**values)


def test_positive_micro_move_is_rejected_when_payoff_is_asymmetric() -> None:
    result = RiskEngine(economic_settings()).build_plan(
        "BTCUSDT",
        decision(100.20, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert not result.allowed
    assert "reward/risk" in result.reason


def test_trade_passes_when_net_target_exceeds_all_in_loss_by_required_ratio() -> None:
    result = RiskEngine(economic_settings()).build_plan(
        "BTCUSDT",
        decision(100.40, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional == pytest.approx(5000)
    assert result.plan.expected_net_profit == pytest.approx(15.75)
    assert result.plan.expected_net_loss == pytest.approx(11.5)
    assert result.plan.net_reward_risk >= 1.15
    economics = result.plan.strategy_details["economics"]
    assert economics["payoffGateEnabled"] is True
    assert economics["requiredNetRewardRisk"] == pytest.approx(1.15)
    assert economics["allInNetLossUsd"] == pytest.approx(11.5)
    assert economics["plannedAllInLossUsd"] == pytest.approx(11.5)
    assert economics["structuralRiskBudgetUsd"] == pytest.approx(5.0)
    assert economics["tradeAllInLossCapUsd"] == pytest.approx(12.5)
    assert economics["riskSizingBasis"] == "structural_stop_with_all_in_cap"
    assert economics["notionalByStructuralRiskUsd"] == pytest.approx(5000)
    assert economics["netRewardRiskRatio"] == pytest.approx(
        result.plan.net_reward_risk
    )
    assert economics["minimumNetRewardRiskRatio"] == pytest.approx(1.15)
    assert economics["payoffMarginUsd"] >= 0


def test_exact_net_reward_risk_boundary_is_accepted() -> None:
    result = RiskEngine(economic_settings()).build_plan(
        "BTCUSDT",
        decision(100.3495, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.expected_net_loss == pytest.approx(11.5)
    assert result.plan.expected_net_profit == pytest.approx(13.225)
    assert result.plan.expected_net_profit == pytest.approx(
        result.plan.expected_net_loss * 1.15
    )


def test_net_reward_risk_just_below_boundary_is_rejected() -> None:
    result = RiskEngine(economic_settings()).build_plan(
        "BTCUSDT",
        decision(100.34948, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert not result.allowed
    assert "economic_gate: insufficient_net_reward_risk" in result.reason


def test_micro_move_is_rejected_when_net_is_only_cents_after_costs() -> None:
    result = RiskEngine(economic_settings()).build_plan(
        "BTCUSDT",
        decision(100.09, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert not result.allowed
    assert "required $1.00" in result.reason


def test_minimum_net_profit_scales_with_equity() -> None:
    cfg = economic_settings(
        start_balance=10_000,
        enforce_net_reward_risk_gate=False,
    )
    engine = RiskEngine(cfg)

    too_small = engine.build_plan(
        "BTCUSDT",
        decision(100.18, stop=99.50),
        10_000,
        book(99.99, 100.00),
        100_000,
        200,
    )
    assert not too_small.allowed
    assert "required $10.00" in too_small.reason

    enough = engine.build_plan(
        "BTCUSDT",
        decision(100.19, stop=99.50),
        10_000,
        book(99.99, 100.00),
        100_000,
        200,
    )
    assert enough.allowed
    assert enough.plan is not None
    assert enough.plan.expected_net_profit >= 10.0


def test_executable_spread_is_not_subtracted_twice() -> None:
    result = RiskEngine(
        economic_settings(enforce_net_reward_risk_gate=False)
    ).build_plan(
        "BTCUSDT",
        decision(100.20, stop=99.90),
        1000,
        book(99.90, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.estimated_costs == pytest.approx(
        result.plan.notional * 0.00085
    )
    economics = result.plan.strategy_details["economics"]
    assert economics["entrySpreadPct"] == pytest.approx(0.0010005, rel=1e-3)
    assert economics["spreadCostDoubleCounted"] is False



def test_structural_risk_fraction_sizes_stop_while_costs_use_separate_cap() -> None:
    cfg = economic_settings(
        risk_fraction=0.005,
        max_trade_all_in_loss_fraction=0.0125,
        max_total_risk_fraction=0.05,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.40, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        50,
    )

    assert result.allowed
    assert result.plan is not None
    economics = result.plan.strategy_details["economics"]
    assert result.plan.notional == pytest.approx(5000)
    assert economics["structuralLossAtStopUsd"] == pytest.approx(5.0)
    assert result.plan.expected_net_loss == pytest.approx(11.5)
    assert result.plan.expected_net_loss <= 12.5 + 1e-9
    assert result.plan.max_loss_usd == pytest.approx(result.plan.expected_net_loss)
    assert economics["riskSizingBasis"] == "structural_stop_with_all_in_cap"


def test_second_tight_stop_trade_is_scaled_by_remaining_all_in_risk() -> None:
    cfg = economic_settings(max_total_risk_fraction=0.02)
    risk = RiskEngine(cfg)
    broker = PaperBroker(cfg)
    market = book(99.99, 100.00)

    first = risk.build_plan(
        "AAAUSDT",
        decision(100.40, stop=99.90),
        broker.balance,
        market,
        broker.available_notional,
        broker.available_risk_usd,
    )
    assert first.allowed
    assert first.plan is not None
    assert first.plan.notional == pytest.approx(5000)
    broker.open(first.plan, market)

    remaining_risk = broker.available_risk_usd
    assert 0 < remaining_risk < 10

    second = risk.build_plan(
        "BBBUSDT",
        decision(100.40, stop=99.90),
        broker.balance,
        market,
        broker.available_notional,
        broker.available_risk_usd,
    )
    assert second.allowed
    assert second.plan is not None
    assert second.plan.notional < first.plan.notional
    assert second.plan.expected_net_loss <= (
        remaining_risk + 1e-9
    )

    broker.open(second.plan, market)
    assert broker.open_risk_usd <= (
        broker.balance * cfg.max_total_risk_fraction + 1e-6
    )


def test_rejects_plan_when_visible_entry_depth_is_too_thin() -> None:
    cfg = economic_settings(max_entry_drift_bps=50)
    thin = OrderBook(
        bids=[(99.99, 1)],
        asks=[(100.00, 1)],
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.40, stop=99.90),
        1000,
        thin,
        10_000,
        20,
    )

    assert not result.allowed
    assert "visible entry depth" in result.reason


def test_depth_vwap_is_used_for_market_entry_and_diagnostics() -> None:
    cfg = economic_settings(
        max_entry_drift_bps=50,
        taker_fee_rate=0,
        slippage_bps=0,
        min_net_profit_usd=0,
        min_net_profit_equity_fraction=0,
        min_net_reward_risk=0,
    )
    layered = OrderBook(
        bids=[(99.99, 100)],
        asks=[(100.00, 1), (100.10, 100)],
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(102.0, stop=99.50),
        1000,
        layered,
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.market_entry > 100.00
    economics = result.plan.strategy_details["economics"]
    assert economics["entryDepthImpactBps"] > 0
    assert economics["visibleEntryDepthUsd"] >= result.plan.notional



def test_research_shadow_economics_allows_positive_net_below_both_legacy_gates() -> None:
    cfg = Settings(
        start_balance=1000,
        risk_fraction=0.005,
        max_total_risk_fraction=0.02,
        max_leverage=10,
        max_position_leverage=5,
        min_net_profit_usd=1.0,
        min_net_profit_equity_fraction=0.001,
        enforce_min_net_profit_gate=False,
        min_net_reward_risk=1.15,
        enforce_net_reward_risk_gate=False,
        taker_fee_rate=0.00055,
        slippage_bps=1.0,
    )
    result = RiskEngine(cfg).build_plan(
        "NEARUSDT",
        decision(100.18, stop=99.45),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert result.allowed
    assert result.plan is not None
    assert 0 < result.plan.expected_net_profit < 1.0
    assert result.plan.net_reward_risk < 1.15
    economics = result.plan.strategy_details["economics"]
    assert economics["economicPolicy"] == "research_shadow"
    assert economics["minimumNetProfitGateEnabled"] is False
    assert economics["payoffGateEnabled"] is False
    assert economics["wouldFailMinimumNetProfit"] is True
    assert economics["wouldFailNetRewardRisk"] is True
    assert economics["shadowRejectReasons"] == [
        "minimum_net_profit",
        "minimum_net_reward_risk",
    ]

def test_research_shadow_still_rejects_non_positive_net() -> None:
    cfg = Settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        min_net_profit_usd=1.0,
        min_net_reward_risk=1.15,
        taker_fee_rate=0.00055,
        slippage_bps=1.0,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.08, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert not result.allowed
    assert "after estimated trading costs" in result.reason



def test_trade_all_in_cap_limits_extremely_cost_heavy_tight_stop() -> None:
    cfg = economic_settings(
        risk_fraction=0.005,
        max_trade_all_in_loss_fraction=0.0125,
        max_position_leverage=10.0,
        max_leverage=10.0,
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        decision(100.50, stop=99.98),
        1000,
        book(99.99, 100.00),
        10_000,
        50,
    )
    assert result.allowed
    assert result.plan is not None
    economics = result.plan.strategy_details["economics"]
    assert result.plan.expected_net_loss <= 12.5 + 1e-6
    assert result.plan.notional <= economics["notionalByTradeAllInCapUsd"] + 1e-6
    assert economics["notionalByStructuralRiskUsd"] > result.plan.notional



def test_target_path_uses_maker_exit_but_stop_path_keeps_taker_costs() -> None:
    cfg = economic_settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["test"],
            entry=100,
            stop=99.9,
            target=100.4,
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    economics = result.plan.strategy_details["economics"]
    assert economics["executionProfile"]["entry"] == "taker_market"
    assert economics["executionProfile"]["target_exit"] == "maker_limit"
    assert economics["targetExitFeeRate"] == pytest.approx(
        cfg.maker_fee_rate
    )
    assert economics["stopExitFeeRate"] == pytest.approx(
        cfg.taker_fee_rate
    )
    assert economics["targetEstimatedCostsUsd"] < economics["stopEstimatedCostsUsd"]



def test_economic_reject_carries_calculated_diagnostics() -> None:
    result = RiskEngine(economic_settings()).build_plan(
        "BTCUSDT",
        decision(100.09, stop=99.90),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert not result.allowed
    assert result.diagnostics is not None
    diagnostics = result.diagnostics
    assert diagnostics["setupEntry"] == pytest.approx(100.0)
    assert diagnostics["stop"] == pytest.approx(99.90)
    assert diagnostics["target"] == pytest.approx(100.09)
    assert diagnostics["grossAtTargetUsd"] > 0
    assert diagnostics["targetEstimatedCostsUsd"] > 0
    assert diagnostics["stopEstimatedCostsUsd"] > diagnostics["targetEstimatedCostsUsd"]
    assert diagnostics["allInNetLossUsd"] > 0
    assert diagnostics["requiredNetProfitUsd"] == pytest.approx(1.0)



def test_breakout_economics_prices_actual_partial_runner_lifecycle() -> None:
    cfg = economic_settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=False,
        enforce_stop_cost_share_gate=False,
        partial_take_at_r=1.0,
        breakout_partial_take_fraction=0.30,
        runner_target_r=2.5,
        maker_fee_rate=0.00020,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["confirmed"],
            entry=100.0,
            stop=99.90,
            target=100.20,
            details={"allowRunner": True},
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert result.allowed
    assert result.plan is not None
    economics = result.plan.strategy_details["economics"]
    assert economics["partialFraction"] == pytest.approx(0.30)
    assert economics["runnerFraction"] == pytest.approx(0.70)
    assert economics["runnerTargetPct"] == pytest.approx(0.0025)
    assert economics["lifecycleGrossPct"] == pytest.approx(0.00205)
    assert economics["lifecycleCostPct"] == pytest.approx(0.00085)
    assert result.plan.expected_gross_profit == pytest.approx(10.25)
    assert result.plan.estimated_costs == pytest.approx(4.25)
    assert result.plan.expected_net_profit == pytest.approx(6.0)
    assert economics["legacyGrossAtFinalTargetUsd"] == pytest.approx(10.0)


def test_winner_cost_share_gate_blocks_fee_dominated_breakout() -> None:
    cfg = economic_settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=True,
        max_winner_cost_share=0.35,
        enforce_stop_cost_share_gate=False,
        breakout_partial_take_fraction=0.30,
        maker_fee_rate=0.00020,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["confirmed"],
            entry=100.0,
            stop=99.90,
            target=100.15,
            details={"allowRunner": True},
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert not result.allowed
    assert "winner_cost_share" in result.reason
    assert result.diagnostics is not None
    assert result.diagnostics["winnerCostShare"] > 0.35


def test_winner_cost_share_gate_accepts_roomy_breakout() -> None:
    cfg = economic_settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=True,
        max_winner_cost_share=0.35,
        enforce_stop_cost_share_gate=False,
        breakout_partial_take_fraction=0.30,
        maker_fee_rate=0.00020,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["confirmed"],
            entry=100.0,
            stop=99.90,
            target=100.40,
            details={"allowRunner": True},
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert result.allowed
    assert result.plan is not None
    economics = result.plan.strategy_details["economics"]
    assert economics["winnerCostShare"] < 0.35



def test_passive_eligible_strategy_prices_maker_entry_when_enabled() -> None:
    cfg = economic_settings(
        passive_entry_enabled=True,
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=False,
        enforce_stop_cost_share_gate=False,
        maker_fee_rate=0.00020,
    )
    result = RiskEngine(cfg).build_plan(
        "DENSUSDT",
        StrategyDecision(
            strategy="orderbook_density",
            action=Action.LONG,
            reasons=["defended wall"],
            entry=100.0,
            stop=99.80,
            target=100.50,
            details={"allowRunner": True},
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )
    assert result.allowed
    assert result.plan is not None
    assert result.plan.entry_mode == "maker_limit"
    assert result.plan.market_entry == pytest.approx(99.99)
    economics = result.plan.strategy_details["economics"]
    assert economics["entryMode"] == "maker_limit"
    assert economics["entryFeeRate"] == pytest.approx(cfg.maker_fee_rate)
    assert economics["entrySlippageRate"] == 0



def test_first_take_movement_gate_rejects_sub_30bps_scalp() -> None:
    cfg = economic_settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=False,
        enforce_min_first_take_move_gate=True,
        min_first_take_move_pct=0.003,
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["confirmed"],
            entry=100.0,
            stop=99.90,
            target=100.80,
            details={"allowRunner": True},
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert not result.allowed
    assert "movement_gate: first_take_move" in result.reason
    assert result.diagnostics is not None
    assert result.diagnostics["firstTakeMovePct"] < 0.003
    assert result.diagnostics["minimumFirstTakeMovePct"] == pytest.approx(0.003)


def test_first_take_movement_gate_accepts_30bps_plus_scalp() -> None:
    cfg = economic_settings(
        enforce_min_net_profit_gate=False,
        enforce_net_reward_risk_gate=False,
        enforce_winner_cost_share_gate=False,
        enforce_min_first_take_move_gate=True,
        min_first_take_move_pct=0.003,
        taker_fee_rate=0,
        maker_fee_rate=0,
        slippage_bps=0,
    )
    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        StrategyDecision(
            strategy="level_breakout",
            action=Action.LONG,
            reasons=["confirmed"],
            entry=100.0,
            stop=99.60,
            target=101.20,
            details={"allowRunner": True},
        ),
        1000,
        book(99.99, 100.00),
        10_000,
        20,
    )

    assert result.allowed
    assert result.plan is not None
    economics = result.plan.strategy_details["economics"]
    assert economics["firstTakeMovePct"] >= 0.003
    assert economics["firstTakeMoveGateEnabled"] is True



def test_staged_probe_and_add_split_structural_risk_budget() -> None:
    cfg = scalp_settings(
        max_position_leverage=10.0,
    )
    engine = RiskEngine(cfg)

    full_decision = decision(
        102.0,
        entry=100.0,
        stop=99.0,
    )
    full = engine.build_plan(
        "BTCUSDT",
        full_decision,
        1000,
        book(99.99, 100.00),
        10_000,
        100,
    )
    assert full.allowed and full.plan is not None

    probe_decision = decision(
        102.0,
        entry=100.0,
        stop=99.0,
    )
    probe_decision.details["stagedEntry"] = {
        "phase": "probe",
        "riskFraction": 0.35,
    }
    probe = engine.build_plan(
        "BTCUSDT",
        probe_decision,
        1000,
        book(99.99, 100.00),
        10_000,
        100,
    )
    assert probe.allowed and probe.plan is not None
    assert probe.plan.notional == pytest.approx(
        full.plan.notional * 0.35
    )
    assert probe.diagnostics["entryRiskFraction"] == pytest.approx(
        0.35
    )

    add_decision = decision(
        102.0,
        entry=100.0,
        stop=99.0,
    )
    add_decision.details["stagedEntry"] = {
        "phase": "add",
        "riskFraction": 0.65,
    }
    add = engine.build_plan(
        "BTCUSDT",
        add_decision,
        1000,
        book(99.99, 100.00),
        10_000 - probe.plan.notional,
        100,
        existing_position_notional=probe.plan.notional,
        existing_position_all_in_risk_usd=(
            probe.plan.expected_net_loss
        ),
    )
    assert add.allowed and add.plan is not None
    assert add.plan.notional == pytest.approx(
        full.plan.notional * 0.65
    )
    assert (
        probe.plan.notional + add.plan.notional
        == pytest.approx(full.plan.notional)
    )


def test_staged_add_respects_remaining_position_exposure_cap() -> None:
    cfg = scalp_settings(
        max_position_leverage=1.0,
        max_position_exposure_fraction=1.0,
        risk_fraction=0.05,
    )
    add_decision = decision(
        110.0,
        entry=100.0,
        stop=99.0,
    )
    add_decision.details["stagedEntry"] = {
        "phase": "add",
        "riskFraction": 0.65,
    }

    result = RiskEngine(cfg).build_plan(
        "BTCUSDT",
        add_decision,
        1000,
        book(99.99, 100.00),
        700,
        100,
        existing_position_notional=700,
        existing_position_all_in_risk_usd=7,
    )

    assert result.allowed
    assert result.plan is not None
    assert result.plan.notional <= pytest.approx(300)
    assert result.diagnostics[
        "remainingPositionExposureCapUsd"
    ] == pytest.approx(300)
