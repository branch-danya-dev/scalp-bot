import asyncio
from dataclasses import asdict
import time

from scalp_bot.ml.contracts import FeatureSnapshot, SnapshotRef
from scalp_bot.ml.native_bridge import NativeChildReplayBridge
from scalp_bot.ml.worker import InferenceWorker
from scalp_bot.native_controlled import NativeControlledDriver
from scalp_bot.native_v5 import PIPELINE, OwnedClock, read_native_tape
from test_native_v5_worker import test_child, writer_at, request
from test_worker_shadow import snapshot, poll_until


async def test_real_spawn_replay_consumes_child_and_relay_clocks_without_blocking_loop(tmp_path):
    snap = snapshot()
    with writer_at(tmp_path/"real.gz") as writer:
        worker = InferenceWorker(tmp_path, _target=test_child, native_endpoint=writer.endpoint("v2", "parent"))
        worker.start()
        try:
            await asyncio.to_thread(poll_until, worker, lambda: worker.ready)
            task = request(writer, snap, "r1")
            worker.activate("AAA", 1)
            worker.submit(snap, "long", native_task=task)
            while not (rows := worker.poll()):
                await asyncio.sleep(.001)
            outputs = dict(forecast=asdict(rows[0][1]))
            worker.complete_native(task.task_id, outputs)
        finally:
            await asyncio.to_thread(worker.close)
    tape = read_native_tape(tmp_path/"real.gz")
    ticks = []
    async def replay(ctx, inputs):
        bridge = NativeChildReplayBridge(ctx.coordinator, tmp_path, target=test_child)
        stop = asyncio.Event()
        async def heartbeat():
            while not stop.is_set():
                ticks.append(time.perf_counter_ns())
                await asyncio.sleep(.001)
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await bridge.start()
            for stage in PIPELINE[:3]:
                await ctx.boundary(stage)
            forecast = await bridge.request(ctx, snap, "long")
            result = dict(forecast=asdict(forecast))
            assert result == outputs
            await ctx.boundary("adapter_decision", result)
            return result
        finally:
            await bridge.close()
            stop.set()
            await heartbeat_task
    result = await NativeControlledDriver(tape, {("v2", "request"):replay}).run("F")
    assert result["realReplayIPC"] and result["consumed"] == len(tape.events)
    assert result["leftovers"] == 0 and result["replayIPCReceipts"][0]["joined"]
    assert len(ticks) > 10
    assert not result["productionCoverage"]
