"""Import a complete archive into a new directory; never amend a capture or raw file."""
from collections import Counter, OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile

from .adapters import ArchiveError, ReadLimits, normalized_events
from .book import BookValidator
from .sources import ArchiveSpec


ADAPTER_VERSION = "external-history-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def import_archive(path: Path, spec: ArchiveSpec, output: Path, *, limits=None, expected_sha256=None) -> dict:
    path, output = Path(path).resolve(), Path(output).absolute()
    if output.exists() or path.is_relative_to(output.resolve()):
        raise FileExistsError("output must be a new directory, not an ancestor of input")
    limits = limits or ReadLimits()
    original = _stamp(path)
    digest = sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ArchiveError("raw SHA256 mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    published = False
    reserved = False
    try:
        counts = Counter()
        book = BookValidator()
        recent_ids = OrderedDict()
        first = last = minimum = maximum = previous_exchange = previous_time = None
        max_silence = 0
        with (temp / "events.jsonl.gz").open("wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb", filename="", mtime=0) as gz:
                with io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as sink:
                    for event in normalized_events(path, spec, limits):
                        counts["events"] += 1
                        counts["rows"] += event["row_end"] - event["row_start"] + 1
                        exchange = event["exchange_time_us"]
                        time = event["available_time_us"] if spec.has_arrival_time else exchange
                        first = time if first is None else first
                        last = time
                        minimum = time if minimum is None else min(minimum, time)
                        maximum = time if maximum is None else max(maximum, time)
                        if previous_time is not None:
                            max_silence = max(max_silence, time - previous_time)
                        if previous_exchange is not None and exchange < previous_exchange:
                            counts["exchange_timestamp_regressions"] += 1
                        previous_time, previous_exchange = time, exchange
                        if event["kind"] == "book_batch":
                            state = book.apply(event)
                            event["reconstruction_state"] = state
                            counts[state] += 1
                            counts["snapshots"] += int(event["is_snapshot"])
                            if state == "uninitialized":
                                counts["rows_before_snapshot"] += event["row_end"] - event["row_start"] + 1
                        else:
                            counts["trades"] += 1
                            counts["unknown_aggressor"] += int(event["side"] == "unknown")
                            trade_id = event["trade_id"]
                            if trade_id:
                                identity = tuple(event[k] for k in ("exchange_time_us", "price", "amount", "side"))
                                if trade_id in recent_ids:
                                    if recent_ids[trade_id] != identity:
                                        raise ArchiveError("conflicting recent trade id; refusing ambiguous history")
                                    counts["duplicate_ids_within_10000"] += 1
                                recent_ids[trade_id] = identity
                                recent_ids.move_to_end(trade_id)
                                if len(recent_ids) > 10_000:
                                    recent_ids.popitem(last=False)
                        sink.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
        if _stamp(path) != original or sha256_file(path) != digest:
            raise ArchiveError("input changed during import")
        warnings = ["exchange sequence/continuity unavailable; gaps are not certified absent",
                    "scanner membership and online feature parity not reconstructed",
                    "archive quantities assume declared linear instrument; metadata still requires review"]
        if not spec.has_arrival_time:
            warnings.append("no collector arrival clock; no exact execution latency or cross-feed ordering")
        else:
            warnings.append("available_time_us is Tardis collector time, not bot receive time")
        suspect = any(counts[k] for k in ("crossed", "one_sided", "duplicate_ids_within_10000", "unknown_aggressor"))
        if not spec.has_arrival_time and counts["exchange_timestamp_regressions"]:
            suspect = True
            warnings.append("exchange timestamps regress in file order; no silent sorting")
        if spec.provider == "tardis-l2" and not counts["snapshots"]:
            suspect = True
            warnings.append("no initial snapshot; no usable reconstructed book")
        manifest = dict(schema_version=1, stage="M1A_ARCHIVE_IMPORT", status="normalized",
                        quality_status="requires_review" if suspect else "structural_checks_passed_only",
                        source=spec.public(), raw_name=path.name, raw_sha256=digest, raw_bytes=original[0],
                        adapter_version=ADAPTER_VERSION,
                        adapter_files={p.name: sha256_file(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
                        created_at=datetime.now(timezone.utc).isoformat(), limits=asdict(limits),
                        counts=dict(sorted(counts.items())), first_time_us=first, last_time_us=last,
                        minimum_time_us=minimum, maximum_time_us=maximum,
                        max_observed_silence_us=max_silence, silence_proves_gap=False,
                        file_order_preserved=True, source_sequence_is_exchange_sequence=False,
                        cross_file_deduplication=False, gzip_eof_checked=path.suffix == ".gz",
                        outputs={"events.jsonl.gz": sha256_file(temp / "events.jsonl.gz")},
                        full_day_coverage_proven=False, capture_replay_compatible=False,
                        training_ready=False, model_trained=False, warnings=warnings)
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        # mkdir reserves the destination without overwriting even an empty directory.
        output.mkdir()
        reserved = True
        (temp / "events.jsonl.gz").replace(output / "events.jsonl.gz")
        (temp / "manifest.json").replace(output / "manifest.json")  # completion marker last
        published = True
        return manifest
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        if reserved and not published:
            shutil.rmtree(output, ignore_errors=True)
