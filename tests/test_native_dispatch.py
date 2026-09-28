import asyncio
from copy import deepcopy

import pytest

from scalp_bot.native_dispatch import NativeDispatch, IngressRegistry
from scalp_bot.native_v5 import NativeTapeError, NativeTapeWriter, provenance, read_native_tape, validate_events
from scalp_bot.runtime_clock import ReplayRuntimeClock


def writer(path):
    return NativeTapeWriter(path, capture_id="production-test",
        provenance=provenance(source_sha256="a"*64, config={}, runtime={}), capacity_bytes=65536)


def test_ingress_identity_independent_of_research_population():
    a, f = IngressRegistry("capture"), IngressRegistry("capture")
    assert a.accept("bybit", "BTCUSDT") == f.accept("bybit", "BTCUSDT")
    for _ in range(100):
        f.accept("okx", "BTCUSDT")
    assert a.accept("bybit", "ETHUSDT") == f.accept("bybit", "ETHUSDT")


async def test_native_actual_resume_and_prestart_cancel(tmp_path):
    path = tmp_path / "tape.gz"
    with writer(path) as tape:
        dispatch = NativeDispatch(tape, clock=ReplayRuntimeClock(wall_seconds=100, mono_ns=123))
        observed = []
        async def child():
            observed.append(dispatch.clock().perf_counter_ns())
            await asyncio.sleep(0)
            observed.append(dispatch.clock().perf_counter_ns())
        async def root():
            task = dispatch.create_task(child(), name="child")
            cancelled = dispatch.create_task(child(), name="cancelled")
            cancelled.cancel()
            await asyncio.gather(task, cancelled, return_exceptions=True)
        await dispatch.run(root())
        await dispatch.join()
    result = read_native_tape(path)
    assert observed == [123, 123]
    assert result.counts["task_open"] == result.counts["task_end"] == 3
    cancelled = [r for r in result.events if r["task_id"].endswith(":cancelled")]
    assert [r["kind"] for r in cancelled] == ["task_open", "task_end"]
    assert cancelled[-1]["data"]["reason"] == "cancelled"
    rows = deepcopy(result.events)
    resumes = [r for r in rows if r["kind"] == "boundary" and r["data"]["name"] == "dispatch_resume"]
    resumes[0]["data"]["value"]["index"] = 77
    with pytest.raises(NativeTapeError, match="resume"):
        validate_events(rows, "production-test")


async def test_unknown_inherited_task_cannot_borrow_clock(tmp_path):
    with writer(tmp_path/"bad.gz") as tape:
        dispatch = NativeDispatch(tape)
        async def outsider():
            dispatch.clock().time()
        async def root():
            with pytest.raises(NativeTapeError, match="ownership"):
                await asyncio.create_task(outsider())
        with pytest.raises(NativeTapeError):
            await dispatch.run(root())
    with pytest.raises(NativeTapeError):
        read_native_tape(tmp_path/"bad.gz")


async def test_first_native_failure_is_not_masked_by_terminal_cleanup(tmp_path):
    with writer(tmp_path/"first-error.gz") as tape:
        dispatch = NativeDispatch(tape)
        async def root():
            dispatch.fail("first-owned-clock-divergence")
        with pytest.raises(NativeTapeError, match="first-owned-clock-divergence"):
            await dispatch.run(root())


async def test_suspend_cancellation_and_nested_clock_modules(tmp_path):
    with writer(tmp_path/"cancel.gz") as tape:
        dispatch = NativeDispatch(tape)
        ready = asyncio.Event()
        async def child():
            with dispatch.scope("segment", "assess"):
                dispatch.clock().time()
            ready.set()
            await asyncio.Event().wait()
        async def root():
            task = dispatch.create_task(child(), name="worker")
            await ready.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await dispatch.run(root())
        await dispatch.join()
    result = read_native_tape(tmp_path/"cancel.gz")
    assert [r["module_id"] for r in result.events if r["kind"] == "clock"] == ["segment"]
    assert any(r["kind"] == "task_end" and r["data"]["reason"] == "cancelled" for r in result.events)
