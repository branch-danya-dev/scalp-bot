import hashlib
import json

import pytest

from scalp_bot.session_validation import _Run, validate_session


def event(name, ts, **payload):
    return {"event": name, "ts": ts, "symbol": "TESTUSDT", "payload": payload}


def frame(ts=11, trade_ms=10_950, observed_ms=11_000, **extra):
    return event("research_frame", ts, marketContext={"formingCandle": {
        "observedAtMs": observed_ms, "lastTradeTsMs": trade_ms,
    }}, **extra)


def start(ts=10):
    return event("bot_started", ts, startedAt=ts, durationSeconds=3, runLabel="test")


def summary(ts=13, net=0, trades=0, **extra):
    return event("run_summary", ts, stoppedAt=ts, elapsedSeconds=3,
                 realizedPnl=net, closedTrades=trades, reason="duration_elapsed",
                 recorderHealth={"droppedRows": 0, "writerError": None}, **extra)


def write(tmp_path, rows):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def codes(report):
    return {finding["code"] for run in report["runs"] for finding in run["findings"]}


def test_clean_run_checks_do_not_certify_edge_or_production(tmp_path):
    path = write(tmp_path, [start(), frame(), summary()])
    report = validate_session(path)
    assert report["status"] == "checks_passed"
    assert report["productionReady"] is False
    assert "strategy_edge" in report["notValidated"]
    assert report["source"]["snapshotSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_aligned_clock_keeps_wall_offset_diagnostic_without_false_rejection(tmp_path):
    clock = {"contract": "exchange-bounds-v1", "valid": True,
             "exchange_lower_ms": 11900, "exchange_upper_ms": 12100, "evaluationMs": 12100}
    path = write(tmp_path, [start(), frame(trade_ms=12000, observed_ms=12100, clock=clock), summary()])
    report = validate_session(path)
    assert report["status"] == "checks_passed"
    assert report["runs"][0]["clock"]["tradeAfterRecord"] == 1
    clock["valid"] = False
    path = write(tmp_path, [start(), frame(clock=clock), summary()])
    assert "runtime_clock_invalid" in codes(validate_session(path))


def test_aligned_clock_never_hides_events_outside_exchange_bounds(tmp_path):
    clock = {"contract": "exchange-bounds-v1", "valid": True,
             "exchange_lower_ms": 11900, "exchange_upper_ms": 12100, "evaluationMs": 12100}
    path = write(tmp_path, [start(), frame(trade_ms=12500, observed_ms=12100, clock=clock), summary()])
    assert "trade_timestamp_after_exchange_bound" in codes(validate_session(path))
    clock["evaluationMs"] = 13000
    path = write(tmp_path, [start(), frame(clock=clock), summary()])
    assert "runtime_clock_invalid_bounds" in codes(validate_session(path))


def test_received_print_ahead_of_clock_is_detected_and_tolerance_is_explicit(tmp_path):
    path = write(tmp_path, [start(), frame(trade_ms=12_100), summary()])
    report = validate_session(path)
    assert report["status"] == "rejected"
    assert {"trade_timestamp_after_record", "trade_timestamp_after_context"} <= codes(report)
    assert report["runs"][0]["clock"]["maxTradeAfterRecordMs"] == 1100
    assert validate_session(path, future_tolerance_ms=1100)["status"] == "checks_passed"


def test_pre_and_post_run_activity_does_not_contaminate_run(tmp_path):
    path = write(tmp_path, [frame(ts=1, trade_ms=100_000), start(), frame(), summary(),
                            event("strategy_toggle", 14, enabled=True),
                            frame(ts=15, trade_ms=100_000, tradeDeltaGap=True)])
    report = validate_session(path)
    assert report["status"] == "checks_passed"
    assert report["runs"][0]["counts"]["research_frame"] == 1


def test_strategy_toggle_and_delta_gap_reject_controlled_comparison(tmp_path):
    path = write(tmp_path, [start(), frame(tradeDeltaGap=True),
                            event("strategy_toggle", 12, strategy="trend_structure", enabled=True), summary()])
    report = validate_session(path)
    assert report["status"] == "rejected"
    assert {"strategy_changed_during_run", "trade_delta_gap"} <= codes(report)


def test_process_cumulative_summaries_reconcile_funding_across_runs(tmp_path):
    trade = event("trade_closed", 12, grossPnl=3, fees=1, fundingPnlUsd=-0.5, netPnl=1.5)
    second = event("trade_closed", 22, grossPnl=-2, fees=1, netPnl=-3)
    path = write(tmp_path, [start(), frame(), trade, summary(net=1.5, trades=1),
                            start(20), frame(ts=21, trade_ms=20_950, observed_ms=21_000),
                            second, summary(ts=23, net=-1.5, trades=2)])
    report = validate_session(path)
    assert report["status"] == "checks_passed"
    assert report["runs"][1]["pnl"]["net"] == -3


def test_trade_and_summary_mismatches_are_independent_findings(tmp_path):
    trade = event("trade_closed", 12, grossPnl=3, fees=1, netPnl=99)
    path = write(tmp_path, [start(), frame(), trade, summary(net=0, trades=2)])
    report = validate_session(path)
    assert {"trade_pnl_mismatch", "summary_pnl_mismatch", "summary_trade_count_mismatch"} <= codes(report)


def test_missing_summary_or_clock_evidence_never_passes(tmp_path):
    path = write(tmp_path, [start(), frame()])
    assert validate_session(path)["status"] == "incomplete"
    path = write(tmp_path, [start(), summary()])
    assert "clock_evidence_unavailable" in codes(validate_session(path))
    path = write(tmp_path, [])
    assert validate_session(path)["status"] == "incomplete"


def test_clock_regression_and_recorder_loss_are_reported(tmp_path):
    end = summary()
    end["payload"]["recorderHealth"]["droppedRows"] = 1
    path = write(tmp_path, [start(), frame(), event("decision", 10.5), end])
    assert {"record_clock_moved_backwards", "recorder_data_loss"} <= codes(validate_session(path))


def test_invalid_row_is_not_silently_skipped(tmp_path):
    path = write(tmp_path, [start(), frame(), summary()])
    with path.open("ab") as stream:
        stream.write(b'{"event":')
    report = validate_session(path)
    assert report["status"] == "rejected"
    assert report["fileIssues"]["invalid_json_row"] == 1


@pytest.mark.parametrize("payload", [
    {"marketContext": [1]}, {"formingCandle": [1]},
])
def test_malformed_frame_context_is_a_finding_not_a_crash(tmp_path, payload):
    path = write(tmp_path, [start(), event("research_frame", 11, **payload), summary()])
    report = validate_session(path)
    assert report["status"] == "rejected"
    assert "invalid_frame_context" in codes(report)


def test_append_during_read_is_excluded_from_hashed_snapshot(tmp_path, monkeypatch):
    path = write(tmp_path, [start(), frame(), summary()])
    original = path.read_bytes()
    consume = _Run.consume

    def append_once(self, row, tolerance_ms):
        if row["event"] == "bot_started":
            with path.open("ab") as stream:
                stream.write(json.dumps(start(20)).encode() + b"\n")
        return consume(self, row, tolerance_ms)

    monkeypatch.setattr(_Run, "consume", append_once)
    report = validate_session(path)
    assert report["status"] == "checks_passed"
    assert len(report["runs"]) == 1
    assert report["source"]["bytesRead"] == len(original)
    assert report["source"]["snapshotSha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_clock_tolerance_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        validate_session(write(tmp_path, []), future_tolerance_ms=value)
