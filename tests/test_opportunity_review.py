from scalp_bot.opportunity_review import analyze_session_rows


def test_rejected_trade_is_classified_by_future_target_stop_order() -> None:
    rows = [
        {
            "ts": 100.0,
            "event": "risk_reject",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "reason": "net reward/risk",
                "decision": {
                    "strategy": "trend_structure",
                    "action": "long",
                    "entry": 100.0,
                    "stop": 99.0,
                    "target": 102.0,
                },
            },
        },
        {
            "ts": 105.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "candle": {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.8,
                    "close": 100.8,
                }
            },
        },
        {
            "ts": 110.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "candle": {
                    "open": 100.8,
                    "high": 102.2,
                    "low": 100.5,
                    "close": 102.0,
                }
            },
        },
    ]

    report = analyze_session_rows(rows, horizon_seconds=60)

    assert report["summary"]["rejectedCandidates"] == 1
    assert report["summary"]["missedTargetFirst"] == 1
    candidate = report["candidates"][0]
    assert candidate["classification"] == "missed_target_first"
    assert candidate["mfeR"] >= 2.0
    assert candidate["maeR"] < 1.0


def test_rejected_trade_ignores_pre_event_forming_candle_extreme_when_tape_exists() -> None:
    rows = [
        {
            "ts": 100.0,
            "event": "risk_reject",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "level_breakout",
                "reason": "economic gate",
                "decision": {
                    "strategy": "level_breakout",
                    "action": "long",
                    "entry": 100.0,
                    "stop": 99.0,
                    "target": 102.0,
                },
            },
        },
        {
            "ts": 105.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                # The 1m high happened before ts=100 and must not be reused
                # as a future target hit.
                "candle": {
                    "open": 100.0,
                    "high": 102.5,
                    "low": 99.8,
                    "close": 100.4,
                },
                "recentTrades": [
                    {
                        "ts": 99_000,
                        "price": 102.5,
                        "size": 1.0,
                        "side": "Buy",
                        "sequence": 1,
                    },
                    {
                        "ts": 105_000,
                        "price": 100.4,
                        "size": 1.0,
                        "side": "Buy",
                        "sequence": 2,
                    },
                ],
            },
        },
    ]

    report = analyze_session_rows(rows, horizon_seconds=60)

    candidate = report["candidates"][0]
    assert candidate["classification"] == "unresolved"
    assert candidate["priceSource"] == "post_event_public_trades"
    assert candidate["mfeR"] < 1.0


def test_rejected_trade_with_stop_first_is_reviewed_as_correct_reject_candidate() -> None:
    rows = [
        {
            "ts": 100.0,
            "event": "setup_blocked",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "level_breakout",
                "reason": "same setup already consumed",
                "decision": {
                    "strategy": "level_breakout",
                    "action": "long",
                    "entry": 100.0,
                    "stop": 99.0,
                    "target": 102.0,
                },
            },
        },
        {
            "ts": 104.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "candle": {
                    "open": 100.0,
                    "high": 100.2,
                    "low": 98.8,
                    "close": 99.1,
                }
            },
        },
    ]

    report = analyze_session_rows(rows, horizon_seconds=60)
    candidate = report["candidates"][0]
    assert candidate["classification"] == "correct_reject_candidate"
    assert candidate["maeR"] >= 1.0


def test_early_exit_is_flagged_when_original_target_hits_after_exit() -> None:
    rows = [
        {
            "ts": 100.0,
            "event": "trade_closed",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "side": "long",
                "entry": 100.0,
                "exit": 100.1,
                "initialStop": 99.0,
                "target": 102.0,
                "netPnl": -0.5,
                "reason": "no_follow_through",
            },
        },
        {
            "ts": 120.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "candle": {
                    "open": 100.1,
                    "high": 102.1,
                    "low": 100.0,
                    "close": 101.9,
                }
            },
        },
    ]

    report = analyze_session_rows(rows, horizon_seconds=60)

    assert report["summary"]["earlyExitReviews"] == 1
    review = report["earlyExits"][0]
    assert review["targetHitAfterExit"] is True
    assert review["postExitMfeR"] >= 2.0



def test_legacy_setup_blocked_uses_latest_recorded_decision() -> None:
    rows = [
        {
            "ts": 95.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "action": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "reasons": ["ready"],
            },
        },
        {
            "ts": 100.0,
            "event": "setup_blocked",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "reason": "same setup already consumed",
            },
        },
        {
            "ts": 110.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "candle": {
                    "open": 100,
                    "high": 102.1,
                    "low": 99.8,
                    "close": 102.0,
                }
            },
        },
    ]

    report = analyze_session_rows(rows, horizon_seconds=60)

    assert report["summary"]["rejectedCandidates"] == 1
    assert report["candidates"][0]["classification"] == "missed_target_first"



def _frame(ts: float, price: float) -> dict:
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


def test_market_move_census_finds_move_even_without_strategy_candidate() -> None:
    rows = [
        _frame(100.0, 100.0),
        _frame(110.0, 100.05),
        _frame(120.0, 100.12),
        _frame(130.0, 100.25),
        _frame(140.0, 100.42),
    ]

    report = analyze_session_rows(
        rows,
        market_move_pct=0.002,
        market_move_horizon_seconds=60.0,
    )

    assert report["summary"]["significantMarketMoves"] == 1
    assert report["summary"]["undetectedMarketMoves"] == 1
    move = report["marketMoves"][0]
    assert move["side"] == "long"
    assert move["visibility"] == "undetected"
    assert move["maxMovePct"] >= 0.004


def test_market_move_census_marks_matching_wait_state_as_observed() -> None:
    rows = [
        _frame(100.0, 100.0),
        {
            "ts": 105.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "level_breakout",
                "action": "wait",
                "confidence": 0.64,
                "reasons": ["holding beyond resistance"],
                "details": {
                    "state": "break",
                    "zone": {
                        "kind": "resistance",
                        "low": 99.9,
                        "high": 100.1,
                    },
                },
            },
        },
        _frame(110.0, 100.12),
        _frame(120.0, 100.25),
        _frame(130.0, 100.35),
    ]

    report = analyze_session_rows(
        rows,
        market_move_pct=0.002,
        market_move_horizon_seconds=60.0,
    )

    move = report["marketMoves"][0]
    assert move["visibility"] == "observed_not_tradeable"
    assert move["matchingWaitStrategies"] == ["level_breakout"]


def test_market_move_census_distinguishes_tradeable_but_unexecuted() -> None:
    rows = [
        _frame(100.0, 100.0),
        {
            "ts": 105.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "action": "long",
                "entry": 100.05,
                "stop": 99.8,
                "target": 100.5,
                "reasons": ["ready"],
                "details": {"state": "continuation"},
            },
        },
        _frame(110.0, 100.12),
        _frame(120.0, 100.26),
    ]

    report = analyze_session_rows(
        rows,
        market_move_pct=0.002,
        market_move_horizon_seconds=60.0,
    )

    move = report["marketMoves"][0]
    assert move["visibility"] == "detected_not_executed"
    assert move["tradeableStrategies"] == ["trend_structure"]


def test_market_move_census_marks_executed_direction_as_traded() -> None:
    rows = [
        _frame(100.0, 100.0),
        {
            "ts": 105.0,
            "event": "trade_opened",
            "symbol": "AAAUSDT",
            "payload": {
                "plan": {
                    "strategy": "level_breakout",
                    "side": "long",
                },
            },
        },
        _frame(110.0, 100.1),
        _frame(120.0, 100.25),
        _frame(130.0, 100.4),
    ]

    report = analyze_session_rows(
        rows,
        market_move_pct=0.002,
        market_move_horizon_seconds=60.0,
    )

    move = report["marketMoves"][0]
    assert move["visibility"] == "traded"
    assert "trade_opened" in move["executionEvents"]


def test_market_move_discovery_uses_last_price_not_stale_forming_high() -> None:
    rows = [
        _frame(100.0, 100.0),
        {
            "ts": 110.0,
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
        _frame(120.0, 100.1),
    ]

    report = analyze_session_rows(
        rows,
        market_move_pct=0.002,
        market_move_horizon_seconds=60.0,
    )

    assert report["summary"]["significantMarketMoves"] == 0
