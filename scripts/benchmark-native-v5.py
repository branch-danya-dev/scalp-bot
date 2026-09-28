"""Fixed offline instrumentation-tax protocol; never a W2 acceptance run."""
import argparse
import asyncio
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scalp_bot.native_v5 import NativeTapeWriter, OwnedClock, read_native_tape, provenance
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.ml.native_fixture import fixture_freeze
from scalp_bot.pipeline_evidence import quantiles

PROTOCOL = dict(schema="native-v5-tax-protocol-1", order=[False, True, True, False],
    batches=160, clocksPerBatch=64, batchPeriodSeconds=.01, ringBytes=1024*1024,
    gcEnabled=True, sampling=False, acceptanceAuthority=False)


async def measure(output, run, recording, frozen):
    path = output/(f"run-{run}.gz")
    writer = NativeTapeWriter(path, capture_id="tax", provenance=frozen["provenance"],
        capacity_bytes=PROTOCOL["ringBytes"]) if recording else None
    task = writer.endpoint("core", "parent").open_task("workload", "callback") if writer else None
    if task: task.start()
    clock = OwnedClock(task) if task else time
    callbacks, lateness, pauses = [], [], []
    gc_start = {}
    def observe_gc(phase, info):
        key = info["generation"]
        if phase == "start": gc_start[key] = time.perf_counter_ns()
        elif key in gc_start: pauses.append((time.perf_counter_ns()-gc_start.pop(key))/1e6)
    gc.callbacks.append(observe_gc)
    started = time.perf_counter()
    error = None
    count = 0
    try:
        for index in range(PROTOCOL["batches"]):
            due = started+index*PROTOCOL["batchPeriodSeconds"]
            await asyncio.sleep(max(0, due-time.perf_counter()))
            before = time.perf_counter_ns()
            lateness.append(max(0, (before/1e9-due)*1000))
            for _ in range(PROTOCOL["clocksPerBatch"]):
                clock.perf_counter_ns()
                count += 1
            callbacks.append((time.perf_counter_ns()-before)/1e6)
        if task: task.end(outputs={"clocks":count})
    except Exception as exc:
        error = type(exc).__name__+": "+str(exc)
    finally:
        gc.callbacks.remove(observe_gc)
        if writer: writer.close()
    elapsed = time.perf_counter()-started
    valid = None
    if writer and error is None:
        tape = read_native_tape(path, expected_provenance=frozen["provenance"])
        valid = tape.counts.get("clock") == count
    return dict(run=run, recording=recording, clocks=count, error=error,
        callbackMs=quantiles(callbacks), nativeLoopLatenessMs=quantiles(lateness),
        callbackTotalMs=sum(callbacks), elapsedSeconds=elapsed, gcPausesMs=quantiles(pauses),
        writer=writer.health() if writer else None, chainAndCountValid=valid,
        allocationThroughput="NOT_TESTED", topAllocationSites="NOT_TESTED")


async def main(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if not gc.isenabled(): raise RuntimeError("GC must remain enabled")
    frozen = fixture_freeze()
    frozen["source"]["fileHashes"]["scripts/benchmark-native-v5.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    frozen["source"]["sourceSha256"] = fingerprint(frozen["source"]["fileHashes"])
    frozen["config"] = dict(protocol=PROTOCOL, fixtureConfig=frozen["config"])
    frozen["provenance"] = provenance(source_sha256=frozen["source"]["sourceSha256"],
        config=frozen["config"], runtime=frozen["runtime"])
    (output/"freeze.json").write_text(json.dumps(dict(protocol=PROTOCOL, freeze=frozen), indent=2)+"\n")
    runs = []
    for i, recording in enumerate(PROTOCOL["order"]):
        runs.append(await measure(output, i, recording, frozen))
        (output/"attempts.json").write_text(json.dumps(runs, indent=2)+"\n")
    baseline = sum(r["callbackTotalMs"] for r in runs if not r["recording"])/2
    recorded = sum(r["callbackTotalMs"] for r in runs if r["recording"])/2
    count = PROTOCOL["batches"]*PROTOCOL["clocksPerBatch"]
    report = dict(protocol=PROTOCOL, runs=runs,
        status="MET" if all(r["error"] is None and r["clocks"] == count and
            (not r["recording"] or r["chainAndCountValid"]) for r in runs) else "NOT_MET",
        taxMeanCallbackMs=recorded-baseline,
        taxMeanMicrosecondsPerClock=(recorded-baseline)*1000/count,
        scope="bounded recorder throughput at 6400 owned clock reads/sec; not production W2 or native A-F attribution",
        controlledW20="NOT_MET")
    (output/"report.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("status", "taxMeanMicrosecondsPerClock", "controlledW20")}))


if __name__ == "__main__":
    for key in list(os.environ):
        if key.upper().startswith("SCALP_"): del os.environ[key]
    os.environ["SCALP_DISABLE_DOTENV"] = "1"
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    asyncio.run(main(parser.parse_args().output))
