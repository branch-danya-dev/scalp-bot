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
