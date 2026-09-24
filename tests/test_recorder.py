import json
import pytest
from scalp_bot.recorder import SessionRecorder


def test_recorder_bounds_bulk_rows_but_keeps_critical_events(tmp_path) -> None:
    recorder = SessionRecorder(
        str(tmp_path),
        max_bulk_pending_rows=1,
    )

    assert recorder._enqueue_background(
        {"event": "research_frame"},
        is_bulk=True,
    )
    assert not recorder._enqueue_background(
        {"event": "market_frame"},
        is_bulk=True,
    )
    assert recorder._enqueue_background(
        {"event": "trade_closed"},
        is_bulk=False,
    )

    health = recorder.health()
    assert health["bulkPendingRows"] == 1
    assert health["maxBulkPendingRows"] == 1
    assert health["droppedRows"] == 1
    assert health["droppedBulkRows"] == 1
    assert health["pendingRows"] == 2


def test_replay_bundle_contains_bootstrap_frames_and_events(tmp_path) -> None:
    recorder = SessionRecorder(str(tmp_path))
    recorder.record("symbol_activated", "AAAUSDT", {"market": {"candles": [{"time": 1, "open": 1, "high": 2, "low": 1, "close": 2}]}})
    recorder.record("market_frame", "AAAUSDT", {"lastPrice": 2, "candle": {"time": 2, "open": 2, "high": 2, "low": 2, "close": 2}, "orderbook": {"bids": [], "asks": []}})
    recorder.record("decision", "AAAUSDT", {"strategy": "test", "action": "wait", "reasons": ["x"]})

    bundle = recorder.replay_bundle(recorder.path.name, "AAAUSDT")
    assert bundle["symbol"] == "AAAUSDT"
    assert len(bundle["bootstrapCandles"]) == 1
    assert len(bundle["frames"]) == 1
    assert [x["event"] for x in bundle["events"]] == ["symbol_activated", "decision"]



def test_trade_review_pairs_open_close_and_preserves_timeline() -> None:
    rows = [
        {
            "ts": 90.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "action": "wait",
                "trace": {
                    "state": "reclaim",
                    "waitingFor": ["follow-through"],
                },
            },
        },
        {
            "ts": 100.0,
            "event": "trade_opened",
            "symbol": "AAAUSDT",
            "payload": {
                "plan": {
                    "strategy": "trend_structure",
                    "side": "long",
                    "setup_id": "trend:aaa:1",
                    "entry": 100.0,
                },
                "market": {
                    "candles": [
                        {"time": 1, "open": 99, "high": 101, "low": 98, "close": 100}
                    ],
                },
            },
        },
        {
            "ts": 105.0,
            "event": "partial_take",
            "symbol": "AAAUSDT",
            "payload": {"netPnl": 2.0},
        },
        {
            "ts": 110.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "lastPrice": 101.0,
                "candle": {"time": 2, "open": 100, "high": 102, "low": 100, "close": 101},
                "orderbook": {"bids": [[100.9, 1]], "asks": [[101.1, 1]]},
            },
        },
        {
            "ts": 120.0,
            "event": "trade_closed",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "trend_structure",
                "side": "long",
                "setupId": "trend:aaa:1",
                "entry": 100.0,
                "exit": 101.5,
                "initialStop": 99.5,
                "target": 101.5,
                "originalNotional": 1000,
                "netPnl": 10.0,
                "grossPnl": 11.0,
                "fees": 1.0,
                "maeUsd": 1.5,
                "mfeUsd": 12.0,
                "maeR": 0.3,
                "mfeR": 2.4,
                "reason": "target",
                "partialTaken": True,
                "openedAt": 100.0,
                "closedAt": 120.0,
                "market": {
                    "candles": [
                        {"time": 2, "open": 100, "high": 102, "low": 100, "close": 101},
                        {"time": 3, "open": 101, "high": 102, "low": 101, "close": 101.5},
                    ],
                },
            },
        },
    ]

    reviews = SessionRecorder._build_trade_reviews(rows)

    assert len(reviews) == 1
    review = reviews[0]
    summary = review["summary"]
    assert summary["symbol"] == "AAAUSDT"
    assert summary["strategy"] == "trend_structure"
    assert summary["netPnl"] == 10.0
    assert summary["durationSeconds"] == 20.0
    assert summary["timelineCount"] == 4
    assert summary["frameCount"] == 1
    assert [row["time"] for row in review["candles"]] == [1, 2, 3]
    assert review["timeline"][0]["payload"]["trace"]["state"] == "reclaim"


def test_trade_review_summaries_and_detail_use_current_session(tmp_path) -> None:
    recorder = SessionRecorder(str(tmp_path))
    recorder.record(
        "trade_opened",
        "AAAUSDT",
        {
            "plan": {
                "strategy": "test",
                "side": "long",
                "setup_id": "setup-1",
            }
        },
    )
    recorder.record(
        "trade_closed",
        "AAAUSDT",
        {
            "strategy": "test",
            "side": "long",
            "setupId": "setup-1",
            "netPnl": 1.25,
        },
    )

    summaries = recorder.trade_review_summaries()
    assert len(summaries) == 1
    review = recorder.trade_review(summaries[0]["reviewId"])
    assert review["summary"]["netPnl"] == 1.25



def test_trade_review_reconstructs_trace_for_legacy_decision_payload() -> None:
    rows = [
        {
            "ts": 90.0,
            "event": "decision",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "weak_level_rejection",
                "action": "wait",
                "reasons": ["waiting"],
                "watched_level": 100.0,
                "details": {
                    "state": "test",
                    "zone": {
                        "kind": "support",
                        "low": 99.9,
                        "high": 100.1,
                    },
                },
            },
        },
        {
            "ts": 100.0,
            "event": "trade_opened",
            "symbol": "AAAUSDT",
            "payload": {
                "plan": {
                    "strategy": "weak_level_rejection",
                    "side": "long",
                    "setup_id": "legacy-1",
                }
            },
        },
        {
            "ts": 110.0,
            "event": "trade_closed",
            "symbol": "AAAUSDT",
            "payload": {
                "strategy": "weak_level_rejection",
                "side": "long",
                "setupId": "legacy-1",
                "netPnl": 1.0,
            },
        },
    ]

    review = SessionRecorder._build_trade_reviews(rows)[0]
    decision = next(
        row
        for row in review["timeline"]
        if row["event"] == "decision"
    )
    trace = decision["payload"]["trace"]
    assert trace["state"] == "test"
    assert trace["object"]["type"] == "horizontal_zone"
    assert trace["object"]["low"] == 99.9


def test_session_report_includes_opportunities_books_charts_coins_and_closed_trades(tmp_path) -> None:
    recorder = SessionRecorder(str(tmp_path))
    recorder.record(
        "bot_started",
        None,
        {
            "config": {
                "economicCalibrationMinGroupSamples": 1,
                "economicCalibrationMinSegmentSamples": 1,
            }
        },
    )
    recorder.record(
        "scanner_update",
        None,
        {
            "active": ["AAAUSDT"],
            "promotedFromTop": ["AAAUSDT"],
            "ranked": [
                {
                    "symbol": "AAAUSDT",
                    "activity_rank": 1,
                    "activity_score": 88.0,
                    "turnover_24h": 250_000_000,
                    "correlation_1h_btc": 0.42,
                }
            ],
        },
    )
    recorder.record(
        "symbol_activated",
        "AAAUSDT",
        {
            "reason": "promoted",
            "semanticArbitration": {
                "allowed": True,
                "confluenceCount": 1,
                "blockers": [],
            },
            "market": {
                "candles": [
                    {"time": 1, "open": 99, "high": 100, "low": 98, "close": 99.5},
                ],
                "chartSeries": {"1m": [], "5m": [], "15m": [], "1h": []},
                "orderbook": {"bids": [[99.4, 2, 198.8]], "asks": [[99.6, 2, 199.2]]},
            },
        },
    )
    recorder.record(
        "decision",
        "AAAUSDT",
        {
            "strategy": "level_breakout",
            "action": "long",
            "entry": 100.0,
            "stop": 99.0,
            "target": 101.0,
            "reasons": ["confirmed"],
        },
    )
    recorder.record(
        "risk_reject",
        "AAAUSDT",
        {
            "strategy": "level_breakout",
            "reason": "test reject",
            "decision": {
                "strategy": "level_breakout",
                "action": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 101.0,
            },
        },
    )
    recorder.record(
        "arbiter_blocked",
        "AAAUSDT",
        {
            "strategy": "level_breakout",
            "setupId": "blocked-1",
            "blockers": [
                "mature_structural_obstacle_before_first_take"
            ],
            "conflictingStrategies": [],
            "semanticArbitration": {
                "allowed": False,
                "confluenceCount": 0,
            },
        },
    )
    recorder.record(
        "research_frame",
        "AAAUSDT",
        {
            "lastPrice": 101.1,
            "candle": {"time": 2, "open": 100, "high": 101.2, "low": 99.9, "close": 101.1},
            "orderbook": {
                "bids": [[101.0, 3, 303.0], [100.9, 2, 201.8]],
                "asks": [[101.2, 3, 303.6], [101.3, 2, 202.6]],
            },
        },
    )
    recorder.record(
        "trade_opened",
        "AAAUSDT",
        {
            "plan": {
                "strategy": "level_breakout",
                "side": "long",
                "setup_id": "setup-1",
                "market_entry": 100.0,
                "stop": 99.0,
                "target": 101.0,
                "net_reward_risk": 1.2,
                "strategy_details": {
                    "entryFreshness": {
                        "classification": "late",
                        "moveSpentRatio": 0.62,
                    },
                    "flowAlignment": {
                        "classification": "short_term_reversal",
                        "score": -0.21,
                    },
                    "liquidityAlignment": {
                        "classification": "supportive",
                        "score": 0.88,
                    },
                    "decisionContext": {
                        "schemaVersion": 1,
                        "htfBias": "bullish",
                        "localRegime": "bullish_trend",
                        "executionReady": True,
                    },
                    "semanticArbitration": {
                        "allowed": True,
                        "confluenceCount": 1,
                        "blockers": [],
                    },
                    "selectionPriority": {
                        "confluenceCount": 1,
                        "flowPriority": 3,
                        "liquidityPriority": 2,
                        "freshnessPriority": 1,
                    },
                    "economics": {
                        "netAtTargetUsd": 12.0,
                        "allInNetLossUsd": 10.0,
                        "winnerCostShare": 0.20,
                        "stopCostShare": 0.30,
                        "firstTakeMovePct": 0.003,
                        "requiredNetProfitUsd": 1.0,
                        "requiredNetRewardRisk": 1.15,
                        "wouldFailMinimumNetProfit": False,
                        "wouldFailNetRewardRisk": False,
                        "wouldFailFirstTakeMove": False,
                        "economicPolicy": "research_shadow",
                    },
                },
            },
            "market": {
                "candles": [
                    {"time": 1, "open": 99, "high": 100, "low": 98, "close": 99.5},
                    {"time": 2, "open": 100, "high": 101.2, "low": 99.9, "close": 101.1},
                ],
                "orderbook": {"bids": [[100, 1, 100]], "asks": [[100.1, 1, 100.1]]},
            },
        },
    )
    recorder.record(
        "market_frame",
        "AAAUSDT",
        {
            "lastPrice": 101.0,
            "candle": {"time": 3, "open": 101.1, "high": 101.3, "low": 100.8, "close": 101.0},
            "orderbook": {"bids": [[100.9, 1, 100.9]], "asks": [[101.1, 1, 101.1]]},
        },
    )
    recorder.record(
        "trade_closed",
        "AAAUSDT",
        {
            "strategy": "level_breakout",
            "side": "long",
            "setupId": "setup-1",
            "entry": 100.0,
            "exit": 101.0,
            "initialStop": 99.0,
            "target": 101.0,
            "originalNotional": 1000,
            "netPnl": 8.5,
            "grossPnl": 10.0,
            "fees": 1.5,
            "maeUsd": 1.0,
            "mfeUsd": 10.0,
            "maeR": 0.1,
            "mfeR": 1.0,
            "initialRiskUsd": 10.0,
            "reason": "target",
            "partialTaken": False,
            "market": {
                "candles": [
                    {"time": 3, "open": 101.1, "high": 101.3, "low": 100.8, "close": 101.0},
                ],
                "orderbook": {"bids": [[100.9, 1, 100.9]], "asks": [[101.1, 1, 101.1]]},
            },
        },
    )
    recorder.record(
        "run_summary",
        None,
        {"runLabel": "test", "closedTrades": 1, "realizedPnl": 8.5},
    )

    report = recorder.session_report()

    assert report["runSummary"]["closedTrades"] == 1
    assert report["closedTrades"][0]["netPnl"] == 8.5
    assert "market" not in report["closedTrades"][0]
    assert report["tradeReviews"][0]["summary"]["symbol"] == "AAAUSDT"
    assert report["tradeReviews"][0]["openSnapshot"]["orderbook"]["bids"]
    assert report["tradeReviews"][0]["closeSnapshot"]["orderbook"]["asks"]
    performance = report["strategySideRegimePerformance"]
    matrix = {
        (row["strategy"], row["side"], row["regime"]): row
        for row in performance["byStrategySideRegime"]
    }
    perf_row = matrix[
        ("level_breakout", "long", "bullish_trend")
    ]
    assert perf_row["trades"] == 1
    assert perf_row["netPnl"] == pytest.approx(8.5)
    assert perf_row["averageEntryMoveSpentRatio"] == pytest.approx(
        0.62
    )
    assert perf_row["flowAlignmentCounts"] == {
        "short_term_reversal": 1
    }

    calibration = report["conditionalEconomicCalibration"]
    assert calibration["summary"]["economicTrades"] == 1
    assert calibration["summary"]["sampleReadyExactGroups"] == 1
    assert calibration["policy"]["minimumGroupSamples"] == 1
    assert calibration["policy"]["minimumSegmentSamples"] == 1
    assert calibration["groups"][0]["sampleReady"] is True
    assert calibration["groups"][0]["baseline"]["expectancyAllInR"] == pytest.approx(0.85)

    diagnostics = report["strategyDiagnostics"]["level_breakout"]
    assert diagnostics["decisionUpdates"] == 1
    assert diagnostics["tradeableDecisionUpdates"] == 1
    assert diagnostics["uniqueTradeableSetups"] == 1
    assert diagnostics["uniqueRiskRejectedSetups"] == 1
    assert diagnostics["tradesOpened"] == 1
    assert diagnostics["arbiterBlockedUpdates"] == 1
    assert diagnostics["arbiterBlockerCounts"]["mature_structural_obstacle_before_first_take"] == 1
    assert diagnostics["selectedConfluenceCounts"]["1"] == 1
    assert diagnostics["entryFreshnessCounts"]["late"] == 1
    assert diagnostics["entryMoveSpentSamples"] == 1
    assert diagnostics["averageEntryMoveSpentRatio"] == 0.62
    assert diagnostics["entryFlowAlignmentCounts"]["short_term_reversal"] == 1
    assert diagnostics["entryFlowAlignmentBySide"]["long"]["short_term_reversal"] == 1
    assert diagnostics["entryFlowScoreSamples"] == 1
    assert diagnostics["averageEntryFlowAlignmentScore"] == -0.21
    assert diagnostics["entryLiquidityAlignmentCounts"]["supportive"] == 1
    assert diagnostics["entryLiquidityAlignmentBySide"]["long"]["supportive"] == 1
    assert diagnostics["entryLiquidityScoreSamples"] == 1
    assert diagnostics["averageEntryLiquidityAlignmentScore"] == 0.88
    assert diagnostics["entryLocalRegimeCounts"]["bullish_trend"] == 1
    assert diagnostics["entryHtfBiasCounts"]["bullish"] == 1
    assert diagnostics["entryExecutionReadyCounts"]["true"] == 1
    assert diagnostics["tradesClosed"] == 1
    assert diagnostics["grossPnl"] == 10.0
    assert diagnostics["fees"] == 1.5
    assert diagnostics["netPnl"] == 8.5
    assert report["postRunOpportunity"]["summary"]["rejectedCandidates"] == 1
    assert report["coins"]["latestRanked"][0]["activity_score"] == 88.0
    market = report["marketData"]["symbols"]["AAAUSDT"]
    assert market["coverage"]["orderbookFrames"] == 2
    assert market["coverage"]["maxBidDepth"] == 2
    assert [row["time"] for row in market["chartCandles"]] == [1, 2, 3]
    assert report["marketData"]["orderbooksStoredInRawFrames"] is True

    output = recorder.write_session_report()
    assert output.exists()
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["session"]["file"] == recorder.path.name
    assert saved["closedTrades"][0]["symbol"] == "AAAUSDT"



def test_current_trade_review_never_rescans_session_file(
    tmp_path,
    monkeypatch,
) -> None:
    recorder = SessionRecorder(str(tmp_path))
    recorder.record(
        "decision",
        "AAAUSDT",
        {
            "strategy": "trend_structure",
            "action": "wait",
            "reasons": ["pre-roll"],
        },
    )
    recorder.record(
        "trade_opened",
        "AAAUSDT",
        {
            "plan": {
                "strategy": "trend_structure",
                "side": "long",
                "setup_id": "live-cache-1",
            },
            "market": {
                "orderbook": {
                    "bids": [[99.9, 1, 99.9]],
                    "asks": [[100.1, 1, 100.1]],
                },
            },
        },
    )
    recorder.record(
        "research_frame",
        "AAAUSDT",
        {
            "lastPrice": 100.2,
            "orderbook": {
                "bids": [[100.1, 1, 100.1]],
                "asks": [[100.3, 1, 100.3]],
            },
        },
    )
    recorder.record(
        "trade_closed",
        "AAAUSDT",
        {
            "strategy": "trend_structure",
            "side": "long",
            "setupId": "live-cache-1",
            "netPnl": 1.5,
            "grossPnl": 2.0,
            "fees": 0.5,
            "market": {
                "orderbook": {
                    "bids": [[100.2, 1, 100.2]],
                    "asks": [[100.4, 1, 100.4]],
                },
            },
        },
    )

    def fail_read(_path):
        raise AssertionError("current trade review must not read raw JSONL")

    monkeypatch.setattr(recorder, "_read_rows", fail_read)

    summaries = recorder.trade_review_summaries()
    assert len(summaries) == 1
    detail = recorder.trade_review(summaries[0]["reviewId"])
    assert detail["summary"]["netPnl"] == 1.5
    assert detail["openSnapshot"]["orderbook"]["bids"]
    assert detail["closeSnapshot"]["orderbook"]["asks"]
    assert detail["frames"]



def test_session_report_aggregates_research_policy_audit(tmp_path) -> None:
    recorder = SessionRecorder(str(tmp_path))
    policy = {
        "mode": "shadow",
        "active": True,
        "policyId": "research-policy-test",
        "version": 2,
        "policyFingerprint": "abc",
        "allowEnforce": False,
        "rules": 1,
    }
    recorder.record(
        "research_policy_activated",
        None,
        policy,
    )
    recorder.record(
        "research_policy_shadow",
        "AAAUSDT",
        {
            "strategy": "trend_structure",
            "setupId": "setup-1",
            "policy": policy,
            "assessment": {
                "policyId": "research-policy-test",
                "policyVersion": 2,
                "mode": "shadow",
                "phase": "pre_plan",
                "strategy": "trend_structure",
                "side": "long",
                "regime": "bullish_trend",
                "matchedRuleIds": ["rule-flow"],
                "wouldBlock": True,
                "blocked": False,
            },
        },
    )
    recorder.record(
        "research_policy_blocked",
        "AAAUSDT",
        {
            "strategy": "trend_structure",
            "setupId": "setup-2",
            "policy": {
                **policy,
                "mode": "enforce",
                "allowEnforce": True,
            },
            "assessment": {
                "policyId": "research-policy-test",
                "policyVersion": 2,
                "mode": "enforce",
                "phase": "post_plan",
                "strategy": "trend_structure",
                "side": "long",
                "regime": "bullish_trend",
                "matchedRuleIds": ["rule-rr"],
                "wouldBlock": True,
                "blocked": True,
            },
        },
    )

    report = recorder.session_report(
        recorder.path.name
    )

    audit = report["researchPolicyAudit"]
    assert len(audit["activations"]) == 1
    assert audit["shadowMatches"] == 1
    assert audit["blockedMatches"] == 1
    assert audit["ruleMatchCounts"] == {
        "rule-flow": 1,
        "rule-rr": 1,
    }
    diagnostics = report["strategyDiagnostics"][
        "trend_structure"
    ]
    assert diagnostics["researchPolicyShadowMatches"] == 1
    assert diagnostics["researchPolicyBlockedMatches"] == 1
    assert diagnostics["researchPolicyRuleMatchCounts"] == {
        "rule-flow": 1,
        "rule-rr": 1,
    }



def test_session_report_tracks_staged_adds_and_fast_path_runtime(
    tmp_path,
) -> None:
    recorder = SessionRecorder(str(tmp_path))
    probe_leg = {
        "phase": "probe",
        "notional": 300.0,
        "fill": 100.0,
        "plan": {
            "expected_net_loss": 4.0,
            "expected_net_profit": 4.0,
        },
    }
    add_leg = {
        "phase": "add",
        "notional": 700.0,
        "fill": 100.285714,
        "plan": {
            "expected_net_loss": 6.0,
            "expected_net_profit": 8.0,
        },
    }
    probe_details = {
        "stagedEntry": {
            "phase": "probe",
            "riskFraction": 0.35,
        },
        "preparedOpportunity": {
            "preparedAtMs": 10_000,
        },
        "fireTrigger": {
            "observedAtMs": 10_500,
        },
        "decisionContext": {
            "localRegime": "bullish_trend",
            "htfBias": "bullish",
            "executionReady": True,
            "analysisRuntime": {
                "mode": "live_fast_path",
            },
        },
        "entryFreshness": {
            "classification": "fresh",
            "moveSpentRatio": 0.10,
            "confirmationAgeSeconds": 0.5,
        },
        "flowAlignment": {
            "classification": "strongly_aligned",
            "score": 0.8,
        },
        "liquidityAlignment": {
            "classification": "supportive",
            "score": 0.7,
        },
    }
    recorder.record(
        "trade_opened",
        "AAAUSDT",
        {
            "plan": {
                "strategy": "level_breakout",
                "side": "long",
                "setup_id": "staged-report-1",
                "market_entry": 100.0,
                "expected_net_loss": 4.0,
                "expected_net_profit": 4.0,
                "strategy_details": probe_details,
            },
            "position": {
                "side": "long",
                "entry": 100.0,
                "setup_id": "staged-report-1",
                "entry_legs": [probe_leg],
            },
            "market": {
                "candles": [{
                    "time": 1,
                    "open": 100.0,
                    "high": 100.1,
                    "low": 99.9,
                    "close": 100.0,
                }],
                "orderbook": {
                    "bids": [[99.99, 1, 99.99]],
                    "asks": [[100.01, 1, 100.01]],
                },
                "analysisRuntime": {
                    "mode": "live_fast_path",
                },
            },
        },
    )
    add_details = {
        **probe_details,
        "stagedEntry": {
            "phase": "add",
            "riskFraction": 0.65,
        },
        "fireTrigger": {
            "observedAtMs": 20_000,
        },
    }
    recorder.record(
        "position_added",
        "AAAUSDT",
        {
            "plan": {
                "strategy": "level_breakout",
                "side": "long",
                "setup_id": "staged-report-1",
                "expected_net_loss": 6.0,
                "expected_net_profit": 8.0,
                "strategy_details": add_details,
            },
            "position": {
                "side": "long",
                "entry": 100.2,
                "setup_id": "staged-report-1",
                "entry_legs": [probe_leg, add_leg],
            },
            "market": {
                "candles": [{
                    "time": 2,
                    "open": 100.0,
                    "high": 100.4,
                    "low": 99.95,
                    "close": 100.2,
                }],
                "orderbook": {
                    "bids": [[100.19, 1, 100.19]],
                    "asks": [[100.21, 1, 100.21]],
                },
                "analysisRuntime": {
                    "mode": "live_fast_path",
                },
            },
        },
    )
    recorder.record(
        "research_frame",
        "AAAUSDT",
        {
            "lastPrice": 100.3,
            "analysisRuntime": {
                "mode": "live_fast_path",
                "staticAnalysisRebuilds": 1,
                "liveFastPathReuses": 4,
            },
            "marketContext": {
                "legacyTrend": "up",
                "executionContext": {"ready": True},
            },
            "orderbook": {
                "bids": [[100.29, 1, 100.29]],
                "asks": [[100.31, 1, 100.31]],
            },
        },
    )
    recorder.record(
        "trade_closed",
        "AAAUSDT",
        {
            "strategy": "level_breakout",
            "side": "long",
            "setupId": "staged-report-1",
            "entry": 100.2,
            "exit": 100.8,
            "initialStop": 99.5,
            "target": 101.0,
            "originalNotional": 1000.0,
            "entryLegs": [probe_leg, add_leg],
            "scaleInCount": 1,
            "strategyDetails": add_details,
            "initialRiskUsd": 10.0,
            "grossPnl": 6.0,
            "fees": 1.0,
            "netPnl": 5.0,
            "mfeR": 1.2,
            "maeR": 0.2,
            "reason": "target",
            "partialTaken": False,
            "market": {
                "candles": [{
                    "time": 3,
                    "open": 100.3,
                    "high": 100.9,
                    "low": 100.2,
                    "close": 100.8,
                }],
                "orderbook": {
                    "bids": [[100.79, 1, 100.79]],
                    "asks": [[100.81, 1, 100.81]],
                },
                "analysisRuntime": {
                    "mode": "live_fast_path",
                },
            },
        },
    )

    report = recorder.session_report()

    trade = report["strategySideRegimePerformance"]["trades"][0]
    assert trade["scaleInCount"] == 1
    assert len(trade["entryLegs"]) == 2
    assert trade["plannedAllInLossUsd"] == pytest.approx(10.0)
    assert trade["plannedNetRewardRisk"] == pytest.approx(1.2)
    assert trade["realizedAllInR"] == pytest.approx(0.5)

    review = report["tradeReviews"][0]
    assert review["summary"]["scaleInCount"] == 1
    assert len(review["summary"]["entryLegs"]) == 2
    assert any(
        row["event"] == "position_added"
        for row in review["timeline"]
    )

    diagnostics = report["strategyDiagnostics"]["level_breakout"]
    assert diagnostics["tradesOpened"] == 1
    assert diagnostics["positionAdds"] == 1
    assert (
        report["marketContextDiagnostics"]["frameCounts"]
        ["analysisMode"]["live_fast_path"]
        == 1
    )


def test_background_recorder_writer_flushes_queued_rows(tmp_path) -> None:
    recorder = SessionRecorder(str(tmp_path))
    recorder.start_background_writer()
    try:
        recorder.record(
            "market_frame",
            "AAAUSDT",
            {
                "lastPrice": 100.0,
                "orderbook": {
                    "bids": [[99.99, 1]],
                    "asks": [[100.01, 1]],
                },
            },
        )
        recorder.flush()

        health = recorder.health()
        assert health["background"] is True
        assert health["writerError"] is None
        assert health["writtenRows"] >= 1
        assert health["pendingRows"] == 0

        bundle = recorder.replay_bundle(
            recorder.path.name,
            "AAAUSDT",
        )
        assert len(bundle["frames"]) == 1
    finally:
        recorder.close()


def test_background_recorder_keeps_live_trade_review_immediate(
    tmp_path,
) -> None:
    recorder = SessionRecorder(str(tmp_path))
    recorder.start_background_writer()
    try:
        recorder.record(
            "trade_opened",
            "AAAUSDT",
            {
                "plan": {
                    "strategy": "test",
                    "side": "long",
                    "setup_id": "async-recorder-1",
                }
            },
        )
        recorder.record(
            "trade_closed",
            "AAAUSDT",
            {
                "strategy": "test",
                "side": "long",
                "setupId": "async-recorder-1",
                "netPnl": 1.0,
            },
        )

        # The review cache is updated synchronously; UI/report consumers don't
        # wait for disk IO even though the JSONL write is queued.
        summaries = recorder.trade_review_summaries()
        assert len(summaries) == 1
        assert summaries[0]["setupId"] == "async-recorder-1"
    finally:
        recorder.close()
