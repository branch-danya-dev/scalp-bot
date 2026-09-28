"""Prospective contract failures must never be admitted as a native proof."""
from copy import deepcopy
import asyncio
import gzip
import json
import multiprocessing as mp
import time

import pytest

from scalp_bot.native_v5 import (
    NativeTapeWriter, NativeTapeError, OwnedClock, read_native_tape,
    schema_hash, provenance, write_sealed_rows,
)
from scalp_bot.native_controlled import NativeControlledDriver


def freeze():
    return provenance(source_sha256="a"*64, config={"duration": 1.0}, runtime={"python": "test"})


SOURCE = dict(capture_id="fixture", symbol="AAAUSDT", epoch=1, source_sequence=7, event_id="m7")


def make_tape(path):
    with NativeTapeWriter(path, capture_id="fixture", provenance=freeze(), capacity_bytes=65536) as writer:
        owner = writer.endpoint("core", "parent")
        task = owner.open_task("market-7", "market", source=SOURCE, inputs={"price": 100})
        task.start()
        clock = OwnedClock(task)
        stamp = clock.perf_counter_ns()
        task.boundary("callback", {"stamp": stamp})
        task.end(outputs={"price": 100})
    return read_native_tape(path, expected_provenance=freeze())


def test_complete_owned_tape_and_footer(tmp_path):
    tape = make_tape(tmp_path/"tape.gz")
    assert tape.counts["clock"] == 1
    assert tape.provenance["schemaSha256"] == schema_hash()
    assert [r["sequence"] for r in tape.events] == list(range(1, len(tape.events)+1))
    assert tape.events[2]["module_id"] == "core"
    assert tape.events[2]["task_id"] == "market-7"


@pytest.mark.parametrize("defect", ["middle", "suffix", "clock_owner", "parent", "duplicate", "unfinished"])
def test_resealed_invalid_contract_is_rejected(tmp_path, defect):
    tape = make_tape(tmp_path/"good.gz")
    rows = deepcopy(tape.events)
    if defect == "middle": rows.pop(2)
    elif defect == "suffix": rows.pop()
    elif defect == "clock_owner": rows[2]["module_id"] = "maker"
    elif defect == "parent": rows[0]["parent_task_id"] = "missing"
    elif defect == "duplicate": rows[2]["sequence"] = rows[1]["sequence"]
    elif defect == "unfinished": rows[-1]["kind"] = "boundary"; rows[-1]["data"] = {"name": "unfinished", "value": {}}
    bad = tmp_path/(defect+".gz")
    write_sealed_rows(bad, tape.header, rows)
    with pytest.raises(NativeTapeError): read_native_tape(bad, expected_provenance=freeze())


def test_tamper_and_truncated_gzip_rejected(tmp_path):
    path = tmp_path/"good.gz"
    make_tape(path)
    rows = [json.loads(line) for line in gzip.open(path, "rt")]
    rows[3]["data"]["value"] += 1
    bad = tmp_path/"tampered.gz"
    with gzip.open(bad, "wt") as out:
        out.write("".join(json.dumps(row)+"\n" for row in rows))
    with pytest.raises(NativeTapeError): read_native_tape(bad)
    bad.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(NativeTapeError): read_native_tape(bad)


def test_exact_provenance_and_version_no_v4_upgrade(tmp_path):
    path = tmp_path/"good.gz"
    make_tape(path)
    wrong = freeze(); wrong["sourceSha256"] = "b"*64
    with pytest.raises(NativeTapeError, match="provenance"):
        read_native_tape(path, expected_provenance=wrong)
    old = tmp_path/"v4.gz"
    with gzip.open(old, "wt") as out: out.write(json.dumps({"schema": "replay-input-v4"})+"\n")
    with pytest.raises(NativeTapeError, match="version"):
        read_native_tape(old)


def spawned_producer(endpoint):
    task = endpoint.open_task("spawn", "callback", inputs={})
    task.start()
    clock = OwnedClock(task)
    for _ in range(40): clock.perf_counter_ns()
    task.end()


def test_spawn_and_parent_share_one_sequence(tmp_path):
    with NativeTapeWriter(tmp_path/"spawn.gz", capture_id="fixture", provenance=freeze(), capacity_bytes=65536) as writer:
        child = mp.get_context("spawn").Process(target=spawned_producer, args=(writer.endpoint("core", "child"),))
        child.start()
        task = writer.endpoint("core", "parent").open_task("parent", "callback")
        task.start()
        clock = OwnedClock(task)
        for _ in range(40): clock.perf_counter_ns()
        task.end()
        child.join(10)
        assert child.exitcode == 0
    tape = read_native_tape(tmp_path/"spawn.gz")
    assert tape.counts["clock"] == 80
    assert {r["producer"] for r in tape.events} == {"parent", "child"}


def test_overflow_poisoned_not_sampled(tmp_path):
    writer = NativeTapeWriter(tmp_path/"full.gz", capture_id="fixture", provenance=freeze(), capacity_bytes=1024)
    try:
        with pytest.raises(NativeTapeError):
            writer.endpoint("core", "parent").open_task("big", "market", inputs={"value": "x"*2000})
    finally: writer.close()
    assert writer.health()["rejected"] == 1
    with pytest.raises(NativeTapeError): read_native_tape(tmp_path/"full.gz")


@pytest.mark.asyncio
async def test_native_driver_exact_owned_clock_and_no_leftovers(tmp_path):
    tape = make_tape(tmp_path/"good.gz")
    async def market(ctx, inputs):
        stamp = await ctx.clock("perf_counter_ns")
        await ctx.boundary("callback", {"stamp": stamp})
        return {"price": inputs["price"]}
    result = await NativeControlledDriver(tape, {("core", "market"): market}).run("A")
    assert result["semanticReplay"] == "MET" and result["leftovers"] == 0
    assert result["controlledW20"] == "NOT_MET"
    async def broken(ctx, inputs): return {"price": inputs["price"]}
    with pytest.raises(NativeTapeError):
        await NativeControlledDriver(tape, {("core", "market"): broken}).run("A")


@pytest.mark.asyncio
async def test_unknown_dispatch_never_silently_consumed(tmp_path):
    tape = make_tape(tmp_path/"good.gz")
    with pytest.raises(NativeTapeError, match="handler"):
        await NativeControlledDriver(tape, {}).run("A")


def test_encoding_failure_poisons_capture_instead_of_omitting_observation(tmp_path):
    with NativeTapeWriter(tmp_path/"invalid.gz", capture_id="fixture", provenance=freeze()) as writer:
        task = writer.endpoint("core", "parent").open_task("bad", "callback")
        task.start()
        with pytest.raises(NativeTapeError): task.boundary("invalid", {"x":float("nan")})
        assert writer.health()["errorCode"] != 0
        assert writer.health()["rejected"] == 1


def test_drain_batch_remains_charged_until_write_acknowledgment():
    from scalp_bot.native_v5 import _Ring
    ring = _Ring(1024)
    ring.put(["core", "parent", "task", None, None, "task_start", {}])
    pending = ring.health()["pendingBytes"]
    batch, done = ring.drain()
    assert batch and not done
    assert ring.health()["pendingBytes"] == pending
    ring.acknowledge(batch)
    assert ring.health()["pendingBytes"] == 0


@pytest.mark.asyncio
async def test_real_engine_and_research_components_repeat_on_same_population(tmp_path):
    from scalp_bot.ml.native_fixture import run_fixture
    report = await run_fixture(tmp_path/"fixture")
    assert report["fixture"] == "MET"
    assert len(report["reports"]) == 12
    assert len({r["ordinaryOperationSha256"] for r in report["reports"]}) == 1
    assert all(r["accountingComplete"] and r["leftovers"] == 0 for r in report["reports"])
    assert report["productionAtoF"] == "NOT_TESTED"


def abandoned_lock(ring):
    import os
    ring.lock.__enter__()
    os._exit(7)


def test_crashed_lock_owner_fails_bounded_not_deadlock():
    from scalp_bot.native_v5 import _Ring
    ring = _Ring(1024)
    child = mp.get_context("spawn").Process(target=abandoned_lock, args=(ring,))
    child.start(); child.join(10)
    assert child.exitcode == 7
    before = time.monotonic()
    with pytest.raises(NativeTapeError, match="lock stalled"):
        ring.put(["core", "parent", "t", None, None, "task_start", {}])
    assert time.monotonic()-before < 2


@pytest.mark.parametrize("reason", ["cancelled", "failed", "shutdown", "coalesced", "dropped", "expired", "inactive"])
def test_terminal_reasons_are_retained(tmp_path, reason):
    with NativeTapeWriter(tmp_path/"terminal.gz", capture_id="fixture", provenance=freeze()) as writer:
        task = writer.endpoint("v2", "parent").open_task("request", "request", source=SOURCE)
        task.start()
        task.boundary("features_ready")
        task.end(reason=reason, outputs={"reason":reason})
    tape = read_native_tape(tmp_path/"terminal.gz")
    assert tape.events[-1]["data"]["reason"] == reason


@pytest.mark.parametrize("defect", ["order", "missing", "duplicate"])
def test_external_enqueue_dequeue_dispatch_contract(tmp_path, defect):
    with NativeTapeWriter(tmp_path/"ext.gz", capture_id="fixture", provenance=freeze()) as writer:
        task = writer.endpoint("cross_venue", "parent").open_task("gap", "gap", source=SOURCE)
        task.start()
        stages = {"order":["dequeue", "enqueue", "dispatch"],
            "missing":["enqueue", "dispatch"], "duplicate":["enqueue", "dequeue", "dequeue", "dispatch"]}[defect]
        for stage in stages: task.boundary(stage)
        task.end()
    with pytest.raises(NativeTapeError, match="external"):
        read_native_tape(tmp_path/"ext.gz")


def test_equal_timestamps_and_multiple_module_clocks_use_tokens(tmp_path):
    from scalp_bot.runtime_clock import ReplayRuntimeClock
    clock = ReplayRuntimeClock(wall_seconds=1000., mono_ns=1234)
    with NativeTapeWriter(tmp_path/"equal.gz", capture_id="fixture", provenance=freeze()) as writer:
        root = writer.endpoint("core", "parent").open_task("root", "callback")
        root.start()
        child = writer.endpoint("maker", "parent").open_task("child", "book", parent_task_id="root")
        child.start()
        for task in (root, child, root, child):
            assert OwnedClock(task, clock).perf_counter_ns() == 1234
        child.end(); root.end()
    tape = read_native_tape(tmp_path/"equal.gz")
    clocks = [r for r in tape.events if r["kind"] == "clock"]
    assert [r["module_id"] for r in clocks] == ["core", "maker", "core", "maker"]
    assert len({r["sequence"] for r in clocks}) == 4


def test_v2_missing_boundary_cannot_be_completed(tmp_path):
    from scalp_bot.native_v5 import PIPELINE
    with NativeTapeWriter(tmp_path/"missing.gz", capture_id="fixture", provenance=freeze()) as writer:
        task = writer.endpoint("v2", "parent").open_task("request", "request", source=SOURCE)
        task.start()
        for stage in PIPELINE[:-1]: task.boundary(stage)
        task.end()
    with pytest.raises(NativeTapeError, match="missing request"):
        read_native_tape(tmp_path/"missing.gz")


@pytest.mark.asyncio
async def test_disabled_parent_dependency_rejected(tmp_path):
    with NativeTapeWriter(tmp_path/"dependency.gz", capture_id="fixture", provenance=freeze()) as writer:
        parent = writer.endpoint("maker", "parent").open_task("parent", "book")
        parent.start()
        child = writer.endpoint("core", "parent").open_task("child", "callback", parent_task_id="parent")
        child.start(); child.end(); parent.end()
    tape = read_native_tape(tmp_path/"dependency.gz")
    with pytest.raises(NativeTapeError, match="disabled module parent"):
        await NativeControlledDriver(tape, {}).run("A")


@pytest.mark.asyncio
async def test_clock_value_types_are_not_normalized(tmp_path):
    tape = make_tape(tmp_path/"types.gz")
    async def handler(ctx, inputs):
        stamp = await ctx.clock("perf_counter_ns")
        await ctx.boundary("callback", {"stamp":float(stamp)})
        return {"price":100}
    with pytest.raises(NativeTapeError, match="mismatch"):
        await NativeControlledDriver(tape, {("core", "market"):handler}).run("A")
