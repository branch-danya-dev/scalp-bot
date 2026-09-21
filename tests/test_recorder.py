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
