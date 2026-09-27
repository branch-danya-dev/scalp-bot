"""M1b1: align verified Tardis imports at collector-time observation fences.

Different feeds sharing a timestamp have no proven cross-feed order. Emit them
as ONE group; consumers must not use book-after/trade-before order for fills.
No exchange client, strategy, training labels or online runtime is involved.
"""
from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Iterator

from .adapters import ArchiveError, timestamp_us
from .book import BookValidator
from .importer import sha256_file
from .sources import ArchiveSpec


@dataclass(frozen=True, slots=True)
class AlignmentLimits:
    max_group_events: int = 50_000
    max_line_bytes: int = 16 * 1024**2
    max_decoded_bytes: int = 8 * 1024**3
    max_output_bytes: int = 8 * 1024**3
    silence_warning_us: int = 5_000_000

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError("alignment limits must be positive integers")


class ImportedStream:
    """One immutable normalized import; verify checksum before and after reading."""
    def __init__(self, directory: Path, provider: str, limits: AlignmentLimits):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "events.jsonl.gz"
        self.manifest_path = self.directory / "manifest.json"
        self.limits = limits
        if self.manifest_path.stat().st_size > 1024**2:
            raise ArchiveError("oversized import manifest")
        self.manifest_bytes = self.manifest_path.read_bytes()
        self.manifest = json.loads(self.manifest_bytes)
        m = self.manifest
        if m.get("schema_version") != 1 or m.get("stage") != "M1A_ARCHIVE_IMPORT" or m.get("status") != "normalized":
            raise ArchiveError("unsupported or incomplete import")
        self.spec = ArchiveSpec.from_public(m["source"])
        if self.spec.provider != provider or not self.spec.has_arrival_time:
            raise ArchiveError("alignment requires Tardis trades and Tardis L2; no invented receive clock")
        count = m["counts"]["events"]
        if type(count) is not int or count <= 0:
            raise ArchiveError("invalid event count")
        for name in ("first_time_us", "last_time_us"):
            value = m[name]
            if type(value) is not int or not self.spec.day_start_us <= value < self.spec.day_start_us + 86_400_000_000:
                raise ArchiveError("invalid import time bounds")
        if m["first_time_us"] > m["last_time_us"]:
            raise ArchiveError("import time bounds regressed")
        self.digest = m["outputs"]["events.jsonl.gz"]
        self.verify()

    def verify(self):
        if self.manifest_path.read_bytes() != self.manifest_bytes or sha256_file(self.path) != self.digest:
            raise ArchiveError("import changed or normalized SHA256 mismatch")

    def events(self) -> Iterator[dict]:
        previous = first = None
        count = decoded = 0
        expected_kind = "book_batch" if self.spec.provider == "tardis-l2" else "trade"
        with gzip.open(self.path, "rb") as stream:
            while line := stream.readline(self.limits.max_line_bytes + 1):
                decoded += len(line)
                if len(line) > self.limits.max_line_bytes or decoded > self.limits.max_decoded_bytes:
                    raise ArchiveError("normalized stream read budget exceeded")
                event = json.loads(line)
                count += 1
                seq, arrival = event.get("source_sequence"), event.get("available_time_us")
                if type(seq) is not int or seq != count or event.get("kind") != expected_kind:
                    raise ArchiveError("unexpected event kind or noncontiguous import sequence")
                if type(arrival) is not int:
                    raise ArchiveError("missing collector timestamp")
                timestamp_us(str(arrival))
                if not self.spec.day_start_us <= arrival < self.spec.day_start_us + 86_400_000_000:
                    raise ArchiveError("collector time outside source partition")
                if previous is not None and arrival < previous:
                    raise ArchiveError("collector order regressed; do not sort/repair")
                first = arrival if first is None else first
                previous = arrival
                yield event
        if count != self.manifest["counts"]["events"] or first != self.manifest["first_time_us"] or previous != self.manifest["last_time_us"]:
            raise ArchiveError("stream does not match manifest counts/bounds")
        self.verify()


def observation_groups(trades: Iterator[dict], books: Iterator[dict], limits: AlignmentLimits) -> Iterator[dict]:
    """Merge sorted feeds, buffering only one collector-time group and lookahead."""
    iterators = {"trades": iter(trades), "book": iter(books)}
    sequence = 0
    try:
        heads = {name: next(it, None) for name, it in iterators.items()}
        while any(event is not None for event in heads.values()):
            now = min(event["available_time_us"] for event in heads.values() if event is not None)
            group = {"trades": [], "book": []}
            total = 0
            for name, it in iterators.items():
                while heads[name] is not None and heads[name]["available_time_us"] == now:
                    total += 1
                    if total > limits.max_group_events:
                        raise ArchiveError("observation group too large; no partial group")
                    group[name].append(heads[name])
                    heads[name] = next(it, None)
            sequence += 1
            yield dict(sequence=sequence, available_time_us=now, **group,
                       cross_feed_order_unknown=bool(group["trades"] and group["book"]))
    finally:
        for it in iterators.values():
            close = getattr(it, "close", None)
            if close:
                close()


def align_archives(trades_directory: Path, book_directory: Path, output: Path, *, limits=None) -> dict:
    """Publish complete grouped observations and a quality report, never a dataset approval."""
    limits = limits or AlignmentLimits()
    trades = ImportedStream(trades_directory, "tardis-trades", limits)
    books = ImportedStream(book_directory, "tardis-l2", limits)
    for key in ("symbol", "day", "market", "purpose"):
        if getattr(trades.spec, key) != getattr(books.spec, key):
            raise ArchiveError(f"source {key} mismatch")
    overlap_start = max(s.manifest["first_time_us"] for s in (trades, books))
    overlap_end = min(s.manifest["last_time_us"] for s in (trades, books))
    if overlap_start >= overlap_end:
        raise ArchiveError("no positive observed time overlap")
    output = Path(output).absolute()
    if output.exists() or any(s.directory.is_relative_to(output.resolve()) or output.resolve().is_relative_to(s.directory)
                              for s in (trades, books)):
        raise FileExistsError("alignment output must be new and outside source directories")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    published = reserved = False
    counts = Counter()
    last_times = {"trades": None, "book": None}
    max_silences = {"trades": 0, "book": 0}
    validator = BookValidator()
    state = "uninitialized"
    written = 0
    try:
        with (temp / "aligned.jsonl.gz").open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz:
                with io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as sink:
                    for group in observation_groups(trades.events(), books.events(), limits):
                        now = group["available_time_us"]
                        counts["groups"] += 1
                        counts["cross_feed_tie_groups"] += int(group["cross_feed_order_unknown"])
                        for name in last_times:
                            counts[f"{name}_events"] += len(group[name])
                            if group[name]:
                                if last_times[name] is not None:
                                    max_silences[name] = max(max_silences[name], now-last_times[name])
                                last_times[name] = now
                        for event in group["book"]:
                            state = validator.apply(event)
                        in_overlap = overlap_start <= now <= overlap_end
                        book_age = None if last_times["book"] is None else now-last_times["book"]
                        group["quality"] = dict(book_state=state, book_age_us=book_age,
                            within_observed_overlap=in_overlap,
                            book_silence_warning=book_age is not None and book_age > limits.silence_warning_us,
                            trade_silence_us=None if last_times["trades"] is None else now-last_times["trades"])
                        counts[f"book_{state}_groups"] += 1
                        counts["outside_overlap_groups"] += int(not in_overlap)
                        text = json.dumps(group, separators=(",", ":"), allow_nan=False)+"\n"
                        written += len(text.encode("utf-8"))
                        if written > limits.max_output_bytes:
                            raise ArchiveError("alignment output budget exceeded")
                        sink.write(text)
        # Final checksum protects an earlier-finished stream until publication.
        trades.verify()
        books.verify()
        report = dict(schema_version=1, stage="M1B1_ALIGNED_OBSERVATIONS", status="complete",
            symbol=trades.spec.symbol, day=trades.spec.day, purpose=trades.spec.purpose,
            quality_status="requires_source_and_coverage_review",
            time_basis="tardis_collector_epoch_microseconds", cross_feed_total_order_proven=False,
            tie_policy="one observation fence; intra-feed order retained; not a fill sequence",
            observed_overlap_us=[overlap_start, overlap_end], counts=dict(sorted(counts.items())),
            max_observed_silence_us=max_silences, silence_warning_us=limits.silence_warning_us,
            silence_proves_gap=False, training_ready=False, full_day_coverage_proven=False,
            capture_replay_compatible=False, contract_multiplier_verified=False,
            sources={name: dict(source=s.spec.public(), manifest_sha256=hashlib.sha256(s.manifest_bytes).hexdigest(),
                               events_sha256=s.digest, raw_sha256=s.manifest["raw_sha256"],
                               quality_status=s.manifest["quality_status"], counts=s.manifest["counts"],
                               warnings=s.manifest["warnings"])
                     for name, s in (("trades", trades), ("book", books))},
            implementation_sha256=sha256_file(Path(__file__)),
            outputs={"aligned.jsonl.gz": sha256_file(temp / "aligned.jsonl.gz")},
            warnings=["collector clock is not the bot receive clock", "overlap is observed bounds, not proof of continuity",
                      "same-time cross-feed order is unknown; not suitable for exact fill ordering",
                      "no metadata approval, feature/label dataset, scanner reconstruction or training"])
        (temp / "alignment-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        output.mkdir()
        reserved = True
        (temp / "aligned.jsonl.gz").replace(output / "aligned.jsonl.gz")
        (temp / "alignment-report.json").replace(output / "alignment-report.json")
        published = True
        return report
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        if reserved and not published:
            shutil.rmtree(output, ignore_errors=True)
