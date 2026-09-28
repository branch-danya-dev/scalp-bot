import time
from scalp_bot.ml.worker import InferenceWorker
from scalp_bot.ml.contracts import ImpulseForecast
from scalp_bot.pipeline_evidence import summarize
from test_worker_shadow import snapshot, poll_until


def timing_worker(model, inbox, outbox, ttl):
    outbox.put(("ready", {}))
    while (task := inbox.get()) is not None:
        snap, side = task[:2]
        start = time.perf_counter_ns()
        end = time.perf_counter_ns()
        forecast = ImpulseForecast(snap.ref, "m", "p", side, 30000, end,
            snap.ref.available_mono_ns+ttl, .7, .2, .1)
        row = ("forecast", forecast, end-start, {})
        if len(task) == 3:
            row += ({"pipelineTiming":dict(task[2], worker_start_ns=start, prediction_end_ns=end)},)
        outbox.put(row)


def test_spawn_pipeline_separates_relay_age_and_preserves_forecast(tmp_path):
    trace = []
    worker = InferenceWorker(tmp_path, trace=trace.append, _target=timing_worker)
    worker.start()
    try:
        poll_until(worker, lambda:worker.ready)
        worker.activate("AAA", 1)
        snap = snapshot()
        worker.submit(snap, "long")
        deadline = time.perf_counter()+5
        while not worker.received.qsize() and time.perf_counter() < deadline:
            time.sleep(.005)
        # Deliberate consumer stall proves its duration belongs to relay age.
        time.sleep(.025)
        result = worker.poll()
        assert result[0][1].source == snap.ref
        assert (result[0][1].p_target_first, result[0][1].side) == (.7, "long")
        row = trace[-1]
        keys = ["available_ns", "submit_ns", "dispatch_ns", "worker_start_ns", "prediction_end_ns",
            "relay_received_ns", "relay_enqueued_ns", "adapter_receive_ns"]
        assert [row[k] for k in keys] == sorted(row[k] for k in keys)
        assert row["adapter_receive_ns"]-row["relay_enqueued_ns"] >= 20_000_000
        assert summarize(trace)["stages"]["relay_queue_wait"]["count"] == 1
    finally:
        worker.close()


def test_unavailable_work_is_retained_in_timing_population(tmp_path):
    trace=[]
    worker=InferenceWorker(tmp_path, trace=trace.append, capacity=1)
    try:
        worker.activate("AAA",1)
        worker.submit(snapshot(1),"long")
        worker.submit(snapshot(2),"long")
        worker.activate("BBB",1)
        worker.submit(snapshot(3,"BBB"),"short")
        worker.deactivate("BBB")
        assert [r["terminal"] for r in trace] == ["coalesced","capacity_dropped","deactivated"]
        assert [r["identity"][3] for r in trace] == [1,2,3]
        assert not worker.timings
    finally:
        worker.close()
