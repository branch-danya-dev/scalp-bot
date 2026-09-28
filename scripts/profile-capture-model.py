"""Bounded historical load with the production compressed input writer enabled."""
import argparse
import asyncio
import gzip
import hashlib
import json
from pathlib import Path

def capture_components(*, clock_reads=False):
    from scalp_bot import offline_benchmark as benchmark
    from scalp_bot.capture import InputWriter
    from scalp_bot.offline_benchmark_trace import frozen_archive
    from scalp_bot.offline_study import source_events
    from scalp_bot.input_journal import InputJournal
    from scalp_bot.input_scope import InputScopes
    from scalp_bot.runtime_clock import RecordingRuntimeClock

    class RecordingLogicalClock(RecordingRuntimeClock):
        def set_observation(self, **kwargs):
            self.source.set_observation(**kwargs)


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
                inputWriterError=self.inputs.error, inputDiagnostics=self.inputs.health())

        def close(self):
            super().close()
            self.inputs.close()


    BaseStudy = benchmark.StudyEngine


    class CaptureStudy(BaseStudy):
        def benchmark_warmup_complete(self):
            # Preserve an undecorated clock for journal envelopes and recorder
            # metadata; observing those would recurse into the journal.
            self.input_journal = InputJournal(self.recorder.record, self.clock)
            self.input_scopes = InputScopes(self.input_journal)
            if clock_reads:
                self.clock = RecordingLogicalClock(self.clock, self._record_clock_read)
                self.broker.clock = self.clock
                # Sessions already exist after warmup. Replacing engine.clock
                # alone silently omits their freshness-clock observations.
                for session in self.sessions.values():
                    session.clock = self.clock


    return benchmark, CaptureBenchmarkRecorder, CaptureStudy, source_events, frozen_archive


async def main(args):
    benchmark, CaptureBenchmarkRecorder, CaptureStudy, source_events, frozen_archive = capture_components(clock_reads=args.clock_reads)
    from scalp_bot.run_manifest import code_provenance
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    source = Path(args.source)
    events = raw_source_events(source) if source.suffix == '.gz' else source_events(source)
    for event in events:
        if not rows:
            first = event["processingMonoNs"]
            manifest = event["body"]["manifest"]
        if event["processingMonoNs"] > first+(args.offset+args.seconds)*1_000_000_000:
            break
        rows.append(event)
    benchmark.BenchmarkRecorder = CaptureBenchmarkRecorder
    benchmark.StudyEngine = CaptureStudy
    before_source = code_provenance(Path(__file__).resolve().parents[1])
    workload_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with frozen_archive():
        result = await benchmark.measure(rows, manifest, output/"run.jsonl", args.model,
            shadow=args.shadow, speed=1, start_ns=first+args.offset*1_000_000_000)
    # measure returns after its finally drains both writers. Do not declare
    # capture healthy from a snapshot taken before shutdown/drain.
    result["recorder_health"] = CaptureBenchmarkRecorder.last_instance.health()
    health = result["recorder_health"]
    result["capture_writer_pass"] = (not health["inputWriterError"]
        and health["inputAccepted"] == health["inputWritten"])
    result.update(sourceCode=before_source,
        sourceUnchanged=code_provenance(Path(__file__).resolve().parents[1])["sourceSha256"] == before_source["sourceSha256"],
        workloadSha256=workload_hash, historicalOffsetSeconds=args.offset, historicalSeconds=args.seconds,
        profilerSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), clockReadCapture=args.clock_reads,
        trainingData=False,
        scope="historical scheduled load; compressed capture with engine/broker/session clock reads" if args.clock_reads else
            "historical scheduled load; compressed market/callback capture; clock-read capture excluded")
    result["scope"] += "; logical scheduler, no live receive/parse, no native replay parity or natural-fill evidence"
    (output/"report.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("events","closed_trades","absolute_event_loop_pass","adapter_budget_pass","capture_writer_pass","recorder_health")}))


def raw_source_events(path):
    kinds = {'manifest', 'bootstrap', 'rest_context', 'scanner_result', 'clock_sample', 'clock_error',
             'market_message', 'transport', 'control', 'symbol_lifecycle', 'run_end'}
    # A failed capture prefix may diagnose load; it is never a valid dataset.
    with gzip.open(path, 'rb') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('event') == 'replay_input' and row['payload']['kind'] in kinds:
                yield row['payload']


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--model")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--offset", type=int, default=120)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--clock-reads", action="store_true", help="Capture engine, broker and warmed-session clock observations")
    asyncio.run(main(parser.parse_args()))
