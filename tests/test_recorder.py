from scalp_bot.recorder import SessionRecorder


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
