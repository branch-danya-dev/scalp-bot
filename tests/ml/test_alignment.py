"""Synthetic archives: causal fences, provenance and bounded failures."""
from dataclasses import replace
import gzip
import json
import subprocess
import sys

import pytest

from scalp_bot.ml.history.adapters import ArchiveError
from scalp_bot.ml.history.alignment import AlignmentLimits, align_archives, observation_groups
from scalp_bot.ml.history.importer import import_archive, sha256_file
from scalp_bot.ml.history.sources import ArchiveSpec

T = 1704067200000000
TRADES = "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
BOOK = "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\n"


def imported(tmp_path, *, book_symbol="NEARUSDT", book_purpose="development"):
    trade_spec = ArchiveSpec("tardis-trades", "NEARUSDT", "2024-01-01")
    book_spec = ArchiveSpec("tardis-l2", book_symbol, "2024-01-01", book_purpose)
    trades = TRADES + "".join(f"bybit,NEARUSDT,{T+e},{T+a},{i},buy,100,1\n"
                              for i, e, a in (("a", 1, 10), ("b", 0, 20), ("c", 30, 40)))
    books = BOOK + "".join(f"bybit,{book_symbol},{T+e},{T+a},{str(snap).lower()},{side},{price},{amount}\n"
                           for e, a, snap, side, price, amount in (
                               (1, 5, False, "bid", 98, 1),
                               (2, 8, True, "bid", 99, 1), (2, 8, True, "ask", 101, 2),
                               (3, 20, False, "bid", 99, 3), (4, 30, False, "ask", 101, 4),
                               (5, 50, False, "bid", 99, 0)))
    for name, spec, text in (("trades", trade_spec, trades), ("book", book_spec, books)):
        raw = tmp_path / f"{name}.gz"
        raw.write_bytes(gzip.compress(text.encode(), mtime=0))
        import_archive(raw, spec, tmp_path / name)
    return tmp_path / "trades", tmp_path / "book"


def output_events(out):
    with gzip.open(out / "aligned.jsonl.gz", "rt") as stream:
        return [json.loads(line) for line in stream]


def rewrite_manifest(directory, fn):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    fn(manifest)
    path.write_text(json.dumps(manifest))


def rewrite_events(directory, fn):
    with gzip.open(directory / "events.jsonl.gz", "rt") as stream:
        rows = [json.loads(x) for x in stream]
    fn(rows)
    (directory / "events.jsonl.gz").write_bytes(gzip.compress(
        "".join(json.dumps(x)+"\n" for x in rows).encode(), mtime=0))
    rewrite_manifest(directory, lambda m: m["outputs"].update({"events.jsonl.gz": sha256_file(directory/"events.jsonl.gz")}))


def test_collector_merge_not_exchange_sort_and_no_fake_fill_order(tmp_path):
    a, b = imported(tmp_path)
    report = align_archives(a, b, tmp_path / "out")
    rows = output_events(tmp_path / "out")
    assert [r["available_time_us"]-T for r in rows] == [5, 8, 10, 20, 30, 40, 50]
    tied = rows[3]
    assert tied["cross_feed_order_unknown"] and len(tied["book"]) == len(tied["trades"]) == 1
    assert tied["trades"][0]["exchange_time_us"] == T  # Regressing exchange clock was retained.
    assert rows[0]["quality"]["book_state"] == "uninitialized"
    assert rows[1]["quality"]["book_state"] == "two_sided_uncrossed"
    assert len(rows[1]["book"][0]["changes"]) == 2  # Whole initial snapshot.
    assert rows[-1]["quality"]["book_state"] == "one_sided"
    assert report["observed_overlap_us"] == [T+10, T+40]
    assert report["counts"]["trades_events"] == 3
    assert report["counts"]["book_events"] == 5
    for key in ("training_ready", "full_day_coverage_proven", "capture_replay_compatible", "cross_feed_total_order_proven"):
        assert report[key] is False
    assert report["outputs"]["aligned.jsonl.gz"] == sha256_file(tmp_path/"out/aligned.jsonl.gz")


def test_repeat_is_deterministic(tmp_path):
    a, b = imported(tmp_path)
    left = align_archives(a, b, tmp_path/"one")
    right = align_archives(a, b, tmp_path/"two")
    assert left == right


@pytest.mark.parametrize("kwargs", [{"book_symbol": "BTCUSDT"}, {"book_purpose": "reserved_holdout"}])
def test_scope_mismatch_is_not_combined(tmp_path, kwargs):
    a, b = imported(tmp_path, **kwargs)
    with pytest.raises(ArchiveError, match="mismatch"):
        align_archives(a, b, tmp_path/"out")
    assert not (tmp_path/"out").exists()


def test_raw_bybit_no_receive_clock_is_not_silently_merged(tmp_path):
    a, b = imported(tmp_path)
    rewrite_manifest(a, lambda m: m.update(source=ArchiveSpec("bybit-public-trades", "NEARUSDT", "2024-01-01").public()))
    with pytest.raises(ArchiveError, match="receive clock"):
        align_archives(a, b, tmp_path/"out")


def test_checksum_failure(tmp_path):
    a, b = imported(tmp_path)
    with (a/"events.jsonl.gz").open("ab") as f:
        f.write(b"broken")
    with pytest.raises(ArchiveError, match="SHA256"):
        align_archives(a, b, tmp_path/"out")


@pytest.mark.parametrize("mutator", [
    lambda r: r[1].update(available_time_us=T+1),
    lambda r: r[1].update(available_time_us=None),
    lambda r: r[1].update(source_sequence=9),
    lambda r: r[1].update(source_sequence=True),
    lambda r: r[1].update(kind="unexpected"),
    lambda r: r.pop(),
])
def test_invalid_normalized_stream_never_published(tmp_path, mutator):
    a, b = imported(tmp_path)
    rewrite_events(a, mutator)
    with pytest.raises(ArchiveError):
        align_archives(a, b, tmp_path/"out")
    assert not (tmp_path/"out").exists()
    assert not list(tmp_path.glob(".out-*"))


@pytest.mark.parametrize("field", ["max_group_events", "max_line_bytes", "max_decoded_bytes", "max_output_bytes"])
def test_resource_budgets(tmp_path, field):
    a, b = imported(tmp_path)
    limits = replace(AlignmentLimits(), **{field: 1})
    with pytest.raises(ArchiveError):
        align_archives(a, b, tmp_path/"out", limits=limits)
    assert not (tmp_path/"out").exists()


@pytest.mark.parametrize("bad", [0, -1, True, 2.5])
def test_bad_limits(bad):
    with pytest.raises(ValueError):
        AlignmentLimits(max_group_events=bad)


@pytest.mark.parametrize("destination", ["existing", "source", "child", "parent"])
def test_no_overwrite_or_mutation_of_input_tree(tmp_path, destination):
    a, b = imported(tmp_path)
    before = (a/"manifest.json").read_bytes()
    out = tmp_path/"existing"
    if destination == "existing": out.mkdir()
    if destination == "source": out = a
    if destination == "child": out = a/"nested"
    if destination == "parent": out = tmp_path
    with pytest.raises(FileExistsError):
        align_archives(a, b, out)
    assert before == (a/"manifest.json").read_bytes()


def test_nonoverlap(tmp_path):
    a, b = imported(tmp_path)
    rewrite_manifest(a, lambda m: m.update(first_time_us=T+60, last_time_us=T+70))
    with pytest.raises(ArchiveError, match="overlap"):
        align_archives(a, b, tmp_path/"out")


def test_silence_is_a_warning_not_proof_of_data_loss(tmp_path):
    a, b = imported(tmp_path)
    report = align_archives(a, b, tmp_path/"out", limits=AlignmentLimits(silence_warning_us=1))
    assert any(r["quality"]["book_silence_warning"] for r in output_events(tmp_path/"out"))
    assert report["silence_proves_gap"] is False


def test_future_extension_does_not_change_prior_groups():
    def event(n, t): return dict(source_sequence=n, available_time_us=t)
    a, b = [event(1, 10), event(2, 20)], [event(1, 5), event(2, 20)]
    past = list(observation_groups(iter(a), iter(b), AlignmentLimits()))
    extended = list(observation_groups(iter(a+[event(3, 50)]), iter(b+[event(3, 40)]), AlignmentLimits()))
    assert extended[:len(past)] == past


def test_cli_has_no_network_and_writes_report(tmp_path):
    a, b = imported(tmp_path)
    result = subprocess.run([sys.executable, "-m", "scalp_bot.ml.history", "align",
        "--trades", str(a), "--book", str(b), "--output", str(tmp_path/"out")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "complete"
