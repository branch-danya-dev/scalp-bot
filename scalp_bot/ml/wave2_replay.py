"""Validate and export supplemental evidence; replay context from normalized events."""
from collections import Counter
import json
from pathlib import Path

from ..cross_venue import CrossVenueRuntime, VenueEvent
from ..research_journal import read_research
from .prepared_learning import digest


def replay(path, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    counts = Counter()
    runtime = None
    labels = []
    prepared = {}
    with (output/"prepared.jsonl").open("x", encoding="utf-8") as snapshots:
        for row in read_research(path):
            kind, body = row["kind"], row["body"]
            counts[kind] += 1
            if kind == "header":
                runtime = CrossVenueRuntime(row["captureId"])
                manifest = body["manifest"]
            elif kind == "venue_event":
                accepted = runtime.ingest(VenueEvent(**body["event"]))
                if accepted != body["accepted"]:
                    raise ValueError("cross-venue replay admission mismatch")
            elif kind == "venue_gap":
                runtime.gap(body["venue"], body["symbol"], body["epoch"], body["reason"])
            elif kind == "prepared":
                source = body["row"]["source"]
                snapshot = runtime.snapshot(source["symbol"], source["available_mono_ns"])
                if snapshot != body["crossVenue"]:
                    raise ValueError("causal prepared cross-venue replay mismatch")
                key = body["row"]["identity"]
                if key in prepared:
                    raise ValueError("duplicate prepared event")
                prepared[key] = body
                snapshots.write(json.dumps(body, separators=(",", ":"))+"\n")
            elif kind == "prepared_label":
                if body["identity"] not in prepared:
                    raise ValueError("label without prior prepared source")
                labels.append(body)
    # read_research has validated the footer by now; export no training-ready
    # dataset manifest until both streams' independent integrity are supplied.
    with (output/"labels.jsonl").open("x", encoding="utf-8") as stream:
        for row in labels:
            stream.write(json.dumps(row, separators=(",", ":"))+"\n")
    result = dict(schema="wave2-export-v1", counts=dict(counts), prepared=len(prepared), labels=len(labels),
        datasetHash=digest(labels), sourceManifest=manifest,
        crossVenueReplay="MET", executableLabelReplay="NOT_TESTED", trainingReady=False,
        requiredGate="primary_capture_integrity_and_label_replay", promotionAuthorized=False)
    (output/"manifest.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result
