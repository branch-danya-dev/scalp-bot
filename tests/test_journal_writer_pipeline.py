import json
import gzip
import pytest
from threading import current_thread

from scalp_bot.input_journal import InputJournal, validate_input_journal
from scalp_bot.recorder import SessionRecorder
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_input_journal import message
from scalp_bot.capture import CaptureRecorder


def test_deferred_public_rows_do_not_retain_the_recorder_or_engine():
    import weakref
    class Sink:
        defer_journal_hashes = True
        def record(self, event, symbol, payload):
            rows.append(payload)
    rows = []
    sink = Sink()
    reference = weakref.ref(sink)
    journal = InputJournal(sink.record, ReplayRuntimeClock(wall_seconds=1000, mono_ns=10))
    del journal, sink
    assert reference() is None
    assert rows[0].resolve()['sequence'] == 1


@pytest.mark.parametrize("compressed", [False, True])
def test_real_capture_hashes_on_fifo_writer_and_detaches_inputs(tmp_path, monkeypatch, compressed):
    import scalp_bot.input_journal as module
    threads = []
    original = module.fingerprint
    def hash_row(row):
        threads.append(current_thread().name)
        return original(row)
    monkeypatch.setattr(module, "fingerprint", hash_row)
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    recorder = (CaptureRecorder if compressed else SessionRecorder)(str(tmp_path), clock=clock)
    recorder.start_background_writer()
    journal = InputJournal(recorder.record, clock)
    try:
        market = message()
        journal.market_message("AAAUSDT", market)
        market.data[0]["p"] = "999"
        journal.append("callback", "AAAUSDT", {"name": "evaluate"})
        journal.close()
        recorder.flush()
        if compressed:
            recorder.inputs.close()
        assert threads and all(name.startswith("recorder:") or name == "paper-input-writer" for name in threads)
        path = recorder.inputs.path if compressed else recorder.path
        content = gzip.open(path, "rt").read() if compressed else path.read_text()
        rows = [json.loads(line) for line in content.splitlines()]
        assert rows[1]["payload"]["body"]["data"][0]["p"] == "100"
        assert [r["payload"]["sequence"] for r in rows] == [1, 2, 3, 4]
        assert validate_input_journal(path)["structuralStatus"] == "checks_passed"
    finally:
        recorder.close()
