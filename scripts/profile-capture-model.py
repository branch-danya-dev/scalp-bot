"""Bounded historical load with the production compressed input writer enabled."""
import argparse
import asyncio
import json
from pathlib import Path

def capture_components():
    from scalp_bot import offline_benchmark as benchmark
    from scalp_bot.capture import InputWriter
    from scalp_bot.offline_benchmark_trace import frozen_archive
    from scalp_bot.offline_study import source_events
    from scalp_bot.input_journal import InputJournal
    from scalp_bot.input_scope import InputScopes


    class CaptureBenchmarkRecorder(benchmark.BenchmarkRecorder):
        last_instance = None

        def __init__(self, path, clock):
            super().__init__(path, clock)
            self.inputs = InputWriter(self.path.with_suffix(".inputs.jsonl.gz"))
            type(self).last_instance = self

        def record(self, event, symbol, payload):
            if event == "replay_input":
                self.inputs.record(event, symbol, payload)
            else:
                super().record(event, symbol, payload)

        def health(self):
            return super().health() | dict(inputAccepted=self.inputs.accepted,
                inputWritten=self.inputs.written, inputPendingBytes=self.inputs.pending_bytes,
                inputWriterError=self.inputs.error)

        def close(self):
            super().close()
            self.inputs.close()


    BaseStudy = benchmark.StudyEngine


    class CaptureStudy(BaseStudy):
        def benchmark_warmup_complete(self):
            # Logical replay clock stays directly controllable; market/callback
            # capture uses the real writer. Clock-read capture overhead is excluded.
            self.input_journal = InputJournal(self.recorder.record, self.clock)
            self.input_scopes = InputScopes(self.input_journal)


    return benchmark, CaptureBenchmarkRecorder, CaptureStudy, source_events, frozen_archive


async def main(args):
    benchmark, CaptureBenchmarkRecorder, CaptureStudy, source_events, frozen_archive = capture_components()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for event in source_events(args.source):
        if not rows:
            first = event["processingMonoNs"]
            manifest = event["body"]["manifest"]
        if event["processingMonoNs"] > first+(args.offset+args.seconds)*1_000_000_000:
            break
        rows.append(event)
    benchmark.BenchmarkRecorder = CaptureBenchmarkRecorder
    benchmark.StudyEngine = CaptureStudy
    with frozen_archive():
        result = await benchmark.measure(rows, manifest, output/"run.jsonl", args.model,
            shadow=args.shadow, speed=1, start_ns=first+args.offset*1_000_000_000)
    # measure returns after its finally drains both writers. Do not declare
    # capture healthy from a snapshot taken before shutdown/drain.
    result["recorder_health"] = CaptureBenchmarkRecorder.last_instance.health()
    health = result["recorder_health"]
    result["capture_writer_pass"] = (not health["inputWriterError"]
        and health["inputAccepted"] == health["inputWritten"])
    result["scope"] = "historical scheduled load; compressed market/callback capture enabled; no live receive/parse or clock-read capture; network causes not reproduced"
    (output/"report.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("events","closed_trades","absolute_event_loop_pass","adapter_budget_pass","capture_writer_pass","recorder_health")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--model")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--offset", type=int, default=120)
    parser.add_argument("--seconds", type=int, default=60)
    asyncio.run(main(parser.parse_args()))
