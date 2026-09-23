from scalp_bot.hindsight_review import analyze_hindsight_opportunities


def frame(ts: float, price: float) -> dict:
    return {
        "ts": ts,
        "event": "research_frame",
        "symbol": "AAAUSDT",
        "payload": {
            "lastPrice": price,
            "candle": {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            },
        },
    }


def activated(ts: float = 0.0) -> dict:
    return {
        "ts": ts,
        "event": "symbol_activated",
        "symbol": "AAAUSDT",
        "payload": {},
    }


def deactivated(ts: float) -> dict:
    return {
        "ts": ts,
        "event": "symbol_deactivated",
        "symbol": "AAAUSDT",
        "payload": {},
    }


def decision(
    ts: float,
    strategy: str,
    state: str,
    *,
    side: str | None = None,
    reason: str = "waiting",
) -> dict:
    payload = {
        "strategy": strategy,
        "action": side or "wait",
        "confidence": 0.7,
        "reasons": [reason],
        "details": {"state": state},
    }
    if strategy == "trend_structure" and side is None:
        payload["trace"] = {
            "state": state,
            "trend": "up",
            "object": {"type": "trendline"},
        }
    return {
        "ts": ts,
        "event": "decision",
        "symbol": "AAAUSDT",
        "payload": payload,
    }


def test_hindsight_finds_profitable_swing_without_any_bot_decision() -> None:
    rows = [
        activated(),
        frame(10.0, 100.0),
        frame(20.0, 100.10),
        frame(30.0, 100.30),
        frame(40.0, 100.55),
        frame(50.0, 100.80),
        deactivated(60.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
        reversal_pct=0.0015,
    )

    assert report["summary"]["opportunities"] == 1
    opportunity = report["opportunities"][0]
    assert opportunity["side"] == "long"
    assert opportunity["oracleEntryPrice"] == 100.0
    assert opportunity["oracleExitPrice"] == 100.8
    assert opportunity["estimatedNetMovePct"] > 0.006
    assert opportunity["strategyFit"]["mappedToExistingStrategy"] is False
    assert opportunity["botComparison"]["classification"] == "missed"


def test_hindsight_filters_move_that_is_not_profitable_after_costs() -> None:
    rows = [
        activated(),
        frame(10.0, 100.0),
        frame(20.0, 100.15),
        frame(30.0, 100.18),
        deactivated(40.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.00055,
        slippage_bps=1.0,
        minimum_net_move_pct=0.001,
        reversal_pct=0.0015,
    )

    assert report["summary"]["opportunities"] == 0


def test_hindsight_maps_opportunity_to_strategy_only_after_market_discovery() -> None:
    rows = [
        activated(),
        decision(5.0, "trend_structure", "pullback"),
        frame(10.0, 100.0),
        decision(15.0, "trend_structure", "test"),
        frame(20.0, 100.10),
        decision(25.0, "trend_structure", "reclaim"),
        frame(30.0, 100.30),
        decision(35.0, "trend_structure", "continuation", side="long"),
        frame(40.0, 100.60),
        frame(50.0, 100.75),
        deactivated(60.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
    )

    opportunity = report["opportunities"][0]
    assert opportunity["strategyFit"]["mappedToExistingStrategy"] is True
    assert opportunity["strategyFit"]["closestPlaybook"] == "trend_structure"
    trend = next(
        row
        for row in opportunity["strategyFit"]["strategies"]
        if row["strategy"] == "trend_structure"
    )
    assert "pullback" in trend["statesSeen"]
    assert "test" in trend["statesSeen"]


def test_hindsight_preserves_latest_state_from_before_oracle_pivot() -> None:
    rows = [
        activated(),
        decision(1.0, "trend_structure", "pullback"),
        frame(100.0, 100.0),
        frame(110.0, 100.30),
        frame(120.0, 100.60),
        deactivated(130.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
    )

    opportunity = report["opportunities"][0]
    trend = next(
        row
        for row in opportunity["strategyFit"]["strategies"]
        if row["strategy"] == "trend_structure"
    )
    assert trend["fit"] == "observed_aligned"
    assert trend["statesSeen"] == ["pullback"]


def test_hindsight_does_not_carry_strategy_state_across_reactivation() -> None:
    rows = [
        activated(0.0),
        decision(1.0, "trend_structure", "continuation", side="long"),
        frame(2.0, 100.0),
        frame(3.0, 100.5),
        deactivated(4.0),
        activated(100.0),
        frame(110.0, 200.0),
        frame(120.0, 200.7),
        deactivated(130.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
    )

    assert report["summary"]["opportunities"] == 2
    second = report["opportunities"][1]
    assert second["segment"] == 2
    assert second["strategyFit"]["mappedToExistingStrategy"] is False


def test_hindsight_compares_actual_entry_and_exit_to_oracle_windows() -> None:
    rows = [
        activated(),
        frame(10.0, 100.0),
        frame(20.0, 100.15),
        {
            "ts": 30.0,
            "event": "trade_opened",
            "symbol": "AAAUSDT",
            "payload": {
                "plan": {
                    "strategy": "trend_structure",
                    "side": "long",
                    "setup_id": "x",
                },
                "position": {
                    "side": "long",
                    "entry": 100.35,
                    "setup_id": "x",
                },
            },
        },
        frame(30.0, 100.35),
        {
            "ts": 35.0,
            "event": "trade_closed",
            "symbol": "AAAUSDT",
            "payload": {
                "setupId": "x",
                "side": "long",
                "exit": 100.40,
                "netPnl": 0.1,
                "reason": "manual",
            },
        },
        frame(40.0, 100.60),
        frame(50.0, 100.80),
        deactivated(60.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
        entry_window_fraction=0.25,
        exit_window_fraction=0.80,
    )

    bot = report["opportunities"][0]["botComparison"]
    assert bot["classification"] in {"late_entry_early_exit", "early_exit"}
    assert bot["entryDelaySeconds"] == 20.0
    assert bot["exitCapturePct"] < 0.8


def test_hindsight_uses_last_price_not_forming_candle_high() -> None:
    rows = [
        activated(),
        frame(10.0, 100.0),
        {
            "ts": 20.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "lastPrice": 100.05,
                "candle": {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.9,
                    "close": 100.05,
                },
            },
        },
        frame(30.0, 100.10),
        deactivated(40.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
    )

    assert report["summary"]["opportunities"] == 0



def test_hindsight_only_analyzes_between_bot_start_and_stop() -> None:
    rows = [
        activated(),
        frame(1.0, 100.0),
        frame(2.0, 101.0),
        {
            "ts": 10.0,
            "event": "bot_started",
            "symbol": None,
            "payload": {
                "config": {
                    "takerFeeRate": 0.0005,
                    "slippageBps": 0.0,
                },
            },
        },
        frame(20.0, 200.0),
        frame(30.0, 200.7),
        {
            "ts": 40.0,
            "event": "run_summary",
            "symbol": None,
            "payload": {"stoppedAt": 40.0},
        },
        frame(50.0, 202.0),
    ]

    report = analyze_hindsight_opportunities(
        rows,
        minimum_net_move_pct=0.001,
    )

    assert report["summary"]["opportunities"] == 1
    opportunity = report["opportunities"][0]
    assert opportunity["oracleEntryTs"] == 20.0
    assert opportunity["oracleExitTs"] == 30.0
    assert report["policy"]["runStartTs"] == 10.0
    assert report["policy"]["runEndTs"] == 40.0


def test_hindsight_uses_recorded_session_execution_costs() -> None:
    rows = [
        activated(),
        {
            "ts": 5.0,
            "event": "bot_started",
            "symbol": None,
            "payload": {
                "config": {
                    "takerFeeRate": 0.001,
                    "slippageBps": 2.0,
                },
            },
        },
        frame(10.0, 100.0),
        frame(20.0, 100.30),
        {
            "ts": 30.0,
            "event": "run_summary",
            "symbol": None,
            "payload": {"stoppedAt": 30.0},
        },
    ]

    report = analyze_hindsight_opportunities(
        rows,
        minimum_net_move_pct=0.001,
    )

    # Costs: 20 bps taker fees + 4 bps slippage = 24 bps.
    # Required gross move is therefore 34 bps, so a 30 bps move is rejected.
    assert report["policy"]["estimatedRoundTripCostPct"] == 0.0024
    assert report["summary"]["opportunities"] == 0


def test_pre_run_activation_generation_matches_strategy_mapping() -> None:
    rows = [
        activated(0.0),
        deactivated(5.0),
        activated(10.0),
        decision(11.0, "trend_structure", "pullback"),
        {
            "ts": 20.0,
            "event": "bot_started",
            "symbol": None,
            "payload": {},
        },
        frame(25.0, 100.0),
        frame(35.0, 100.4),
        {
            "ts": 40.0,
            "event": "run_summary",
            "symbol": None,
            "payload": {"stoppedAt": 40.0},
        },
    ]

    report = analyze_hindsight_opportunities(
        rows,
        taker_fee_rate=0.0005,
        slippage_bps=0.0,
        minimum_net_move_pct=0.001,
    )

    opportunity = report["opportunities"][0]
    assert opportunity["segment"] == 2
    assert opportunity["strategyFit"]["mappedToExistingStrategy"] is True
