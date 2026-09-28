"""Production parent/spawn/relay path with a deterministic predictor test double."""
from dataclasses import asdict
import time

import pytest

from scalp_bot.ml.worker import InferenceWorker
from scalp_bot.ml.contracts import ImpulseForecast
from scalp_bot.native_v5 import NativeTapeWriter, OwnedClock, PIPELINE, read_native_tape, provenance
from test_worker_shadow import snapshot, poll_until


def test_child(model_dir, inbox, outbox, ttl_ns, endpoint):
    outbox.put(("ready", {}))
    while (row := inbox.get()) is not None:
        snap, side, timing = row
        task = endpoint.attach_task(**timing["nativeTask"])
        task.boundary("worker_receive"); task.boundary("worker_start")
        clock = OwnedClock(task)
        started = clock.perf_counter_ns()
        end = clock.perf_counter_ns()
        task.boundary("prediction_complete", {"probabilities":[.7,.2,.1]})
        forecast = ImpulseForecast(snap.ref, "m", "p", side, 30000, end,
            snap.ref.available_mono_ns+ttl_ns, .7,.2,.1)
        task.boundary("reply_ipc_enqueue")
        outbox.put(("forecast", forecast, end-started, {}, {"pipelineTiming":timing}))


test_child.__test__ = False


def writer_at(path):
    return NativeTapeWriter(path, capture_id="test", provenance=provenance(
        source_sha256="a"*64, config={}, runtime={}))


def request(writer, snap, identity):
    source = dict(capture_id=snap.ref.capture_id, symbol=snap.ref.symbol,
        epoch=snap.ref.selection_epoch, source_sequence=snap.ref.source_sequence, event_id="m"+identity)
    task = writer.endpoint("v2", "parent").open_task(identity, "request", source=source)
    task.start()
    for stage in PIPELINE[:3]: task.boundary(stage)
    return task


def test_owned_capture_covers_real_spawn_queue_relay_and_adapter(tmp_path):
    with writer_at(tmp_path/"worker.gz") as writer:
        worker = InferenceWorker(tmp_path, _target=test_child, native_endpoint=writer.endpoint("v2", "parent"))
        worker.start()
        try:
            poll_until(worker, lambda: worker.ready)
            snap = snapshot()
            task = request(writer, snap, "r1")
            worker.activate("AAA", 1)
            worker.submit(snap, "long", native_task=task)
            deadline = time.monotonic()+5
            result = []
            while not result and time.monotonic() < deadline:
                result = worker.poll()
                time.sleep(.002)
            assert result[0][1].source == snap.ref
            worker.complete_native("r1", {"probabilities":[.7,.2,.1]})
        finally: worker.close()
    tape = read_native_tape(tmp_path/"worker.gz")
    assert [r["data"]["name"] for r in tape.events if r["kind"] == "boundary"] == list(PIPELINE)
    assert {r["producer"] for r in tape.events} == {"parent", "inference-worker", "reply-relay"}
    assert not worker.process.is_alive() and not worker.receiver.is_alive()


def test_all_unavailable_requests_receive_terminal_evidence(tmp_path):
    with writer_at(tmp_path/"terminals.gz") as writer:
        worker = InferenceWorker(tmp_path, capacity=1, native_endpoint=writer.endpoint("v2", "parent"))
        try:
            worker.activate("AAA", 1)
            for seq in (1, 2):
                snap = snapshot(seq)
                worker.submit(snap, "long", native_task=request(writer, snap, str(seq)))
            worker.activate("BBB", 1)
            snap = snapshot(3, "BBB")
            worker.submit(snap, "long", native_task=request(writer, snap, "3"))
            worker.deactivate("BBB")
            snap = snapshot(4, "AAA")
            worker.submit(snap, "long", native_task=request(writer, snap, "4"))
        finally: worker.close()
    tape = read_native_tape(tmp_path/"terminals.gz")
    assert [r["data"]["reason"] for r in tape.events if r["kind"] == "task_end"] == [
        "coalesced", "dropped", "inactive", "shutdown"]


def test_poisoned_tape_still_joins_worker_and_relay(tmp_path):
    writer = writer_at(tmp_path/"poisoned.gz")
    worker = InferenceWorker(tmp_path, _target=test_child, native_endpoint=writer.endpoint("v2", "parent"))
    worker.start()
    try:
        poll_until(worker, lambda:worker.ready)
        snap = snapshot()
        task = request(writer, snap, "r1")
        worker.activate("AAA", 1)
        worker.submit(snap, "long", native_task=task)
        with writer.ring.lock: writer.ring.state[4] = 8
    finally:
        worker.close()
        writer.close()
    assert not worker.process.is_alive() and not worker.receiver.is_alive()
    assert worker.native_error
    with pytest.raises(ValueError): read_native_tape(tmp_path/"poisoned.gz")
