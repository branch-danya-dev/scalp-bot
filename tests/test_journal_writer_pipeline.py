import json
import gzip
import pytest
from threading import current_thread

from scalp_bot.input_journal import InputJournal, validate_input_journal
from scalp_bot.recorder import SessionRecorder
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_input_journal import message
from scalp_bot.capture import CaptureRecorder


def test_burst_uses_bounded_compression_batches_and_preserves_every_hash(tmp_path, monkeypatch):
    from threading import Event
    from scalp_bot.capture import InputWriter
    from scalp_bot.input_journal import DeferredJournalRow, JournalHashChain
    import msgspec
    ready = Event()
    real_run = InputWriter._run
    monkeypatch.setattr(InputWriter, '_run', lambda self: (ready.wait(5), real_run(self)))
    writer = InputWriter(tmp_path/'inputs.gz')
    expected = []
    chain = JournalHashChain()
    writer_chain = JournalHashChain()
    try:
        for sequence in range(1, 3001):
            row = dict(kind='clock_read', body=dict(method='time', value=123.0, scopeId=1),
                sequence=sequence)
            expected.append(dict(event='replay_input', symbol=None,
                payload=DeferredJournalRow(dict(row), chain, 4096).resolve()))
            writer.record('replay_input', None, DeferredJournalRow(row, writer_chain, 4096))
    finally:
        ready.set()
        writer.close()
    assert writer.error is None
    assert writer.accepted == writer.written == 3000
    assert writer.pending_bytes == 0
    assert gzip.decompress(writer.path.read_bytes()) == b''.join(msgspec.json.encode(r)+b'\n' for r in expected)
    # A ready burst must not invoke the compressor separately for every clock read.
    assert 1 < writer.compression_writes <= 5
    assert writer.codec.pid != __import__('os').getpid()
    assert not writer.codec.process.is_alive()


def test_batch_bytes_stay_charged_while_write_is_blocked(tmp_path, monkeypatch):
    from threading import Event
    from scalp_bot.capture import InputWriter
    from scalp_bot.input_journal import DeferredJournalRow, JournalHashChain
    entered, release = Event(), Event()
    writer = InputWriter(tmp_path/'inputs.gz', max_queue_bytes=4096)
    original = writer.codec.encode
    def blocked(data):
        entered.set()
        assert release.wait(5)
        return original(data)
    monkeypatch.setattr(writer.codec, 'encode', blocked)
    try:
        writer.record('replay_input', None, DeferredJournalRow(dict(kind='test', body={}), JournalHashChain(), 4096))
        assert entered.wait(5)
        assert writer.pending_bytes == 4096
        writer.record('replay_input', None, DeferredJournalRow(dict(kind='test', body={}), JournalHashChain(), 4096))
        assert 'byte limit' in writer.error
        assert writer.rejected == 1
    finally:
        release.set()
        writer.close()
    assert writer.accepted == writer.written == 1
    assert writer.pending_bytes == 0


def test_failed_batch_never_counts_as_written(tmp_path, monkeypatch):
    from scalp_bot.capture import InputWriter
    writer = InputWriter(tmp_path/'inputs.gz')
    def failed(data):
        raise OSError('injected codec failure')
    monkeypatch.setattr(writer.codec, 'encode', failed)
    writer.record('replay_input', None, dict(kind='footer', body={}))
    writer.close()
    assert 'OSError' in writer.error
    assert writer.accepted == 1 and writer.written == 0
    assert writer.pending_bytes == 0
    assert not writer.thread.is_alive()
    assert not writer.codec.process.is_alive()


def test_codec_process_death_invalidates_capture_and_shutdown_is_bounded(tmp_path):
    from scalp_bot.capture import InputWriter
    writer = InputWriter(tmp_path/'inputs.gz')
    writer.codec.process.terminate()
    writer.codec.process.join(2)
    writer.record('replay_input', None, dict(kind='footer', body={}))
    writer.close()
    assert writer.error and writer.accepted == 1 and writer.written == 0
    assert not writer.thread.is_alive() and not writer.codec.process.is_alive()


def test_codec_receipts_preserve_independent_chains_across_batches(tmp_path):
    from scalp_bot.capture import InputWriter
    from scalp_bot.input_journal import DeferredJournalRow, JournalHashChain
    writer = InputWriter(tmp_path/'inputs.gz')
    chains = [JournalHashChain(), JournalHashChain()]
    golden = [JournalHashChain(), JournalHashChain()]
    expected = []
    try:
        for index in range(2500):
            key = index % 2
            row = dict(kind='test', body=dict(index=index, chain=key))
            expected.append(DeferredJournalRow(dict(row), golden[key], 4096).resolve())
            writer.record('test', None, DeferredJournalRow(row, chains[key], 4096))
    finally:
        writer.close()
    assert not writer.error
    assert [json.loads(line)['payload'] for line in gzip.decompress(writer.path.read_bytes()).splitlines()] == expected
    assert [chain.previous_hash for chain in chains] == [chain.previous_hash for chain in golden]


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
        if compressed:
            assert not threads, "compressed capture must not hash on the market process GIL"
            assert recorder.inputs.health()['hashedRows'] == 4
            assert recorder.inputs.codec.pid != __import__('os').getpid()
        else:
            assert threads and all(name.startswith("recorder:") for name in threads)
        path = recorder.inputs.path if compressed else recorder.path
        content = gzip.open(path, "rt").read() if compressed else path.read_text()
        rows = [json.loads(line) for line in content.splitlines()]
        assert rows[1]["payload"]["body"]["data"][0]["p"] == "100"
        assert [r["payload"]["sequence"] for r in rows] == [1, 2, 3, 4]
        assert validate_input_journal(path)["structuralStatus"] == "checks_passed"
    finally:
        recorder.close()
