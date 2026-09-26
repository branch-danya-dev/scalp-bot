"""Synthetic source-format fixtures only; no external network in the test suite."""
from dataclasses import replace
from decimal import Decimal
import gzip
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scalp_bot.ml.history.adapters import ArchiveError, ReadLimits, decimal_text, normalized_events, timestamp_us
from scalp_bot.ml.history.book import BookValidator
from scalp_bot.ml.history.importer import import_archive, sha256_file
from scalp_bot.ml.history.sources import ArchiveSpec


DAY = "2024-01-01"
T = 1704067200000000
BYBIT = "timestamp,symbol,side,size,price,trdMatchID\n"
TRADES = "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
L2 = "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\n"


def spec(provider="bybit-public-trades"):
    return ArchiveSpec(provider, "NEARUSDT", DAY, "format_smoke_only")


def save(tmp_path, text, compressed=True):
    path = tmp_path / ("source.csv.gz" if compressed else "source.csv")
    path.write_bytes(gzip.compress(text.encode(), mtime=0) if compressed else text.encode())
    return path


def row(side="Buy", price="3.123456789123", amount="2", id="A", at="1704067200.123456"):
    return f"{at},NEARUSDT,{side},{amount},{price},{id}\n"


def bookrow(local=10, side="bid", price="100", amount="1", snapshot=False, exchange=0):
    return f"bybit,NEARUSDT,{T+exchange},{T+local},{str(snapshot).lower()},{side},{price},{amount}\n"


def read_output(path):
    with gzip.open(path / "events.jsonl.gz", "rt") as stream:
        return [json.loads(x) for x in stream]


@pytest.mark.parametrize("provider", ["bybit-public-trades", "tardis-trades", "tardis-l2"])
def test_source_round_trip_and_no_implicit_availability(provider):
    s = spec(provider)
    assert ArchiveSpec.from_public(s.public()) == s
    assert not s.public()["availability_verified"]
    assert not s.public()["contract_multiplier_verified"]
    assert s.url.startswith("https://")
    assert "2024" in s.url


@pytest.mark.parametrize("change", [{"provider": "binance"}, {"symbol": "BTCUSD"}, {"symbol": "../NEARUSDT"},
                                   {"day": "20240101"}, {"day": "2024-02-30"}, {"market": "spot"},
                                   {"schema_version": True}, {"purpose": "profit_optimized"}])
def test_reject_source_scope(change):
    with pytest.raises(ValueError):
        replace(spec(), **change)


def test_descriptor_cannot_embed_secret_or_redirect():
    value = spec().public()
    value["url"] = "https://evil.test/file?api_key=secret"
    with pytest.raises(ValueError):
        ArchiveSpec.from_public(value)


@pytest.mark.parametrize("compressed", [False, True])
def test_bybit_precision_and_no_invented_arrival(tmp_path, compressed):
    raw = save(tmp_path, BYBIT + row(), compressed)
    m = import_archive(raw, spec(), tmp_path / "out")
    event = read_output(tmp_path / "out")[0]
    assert event["exchange_time_us"] == T + 123456
    assert event["available_time_us"] is None
    assert event["price"] == "3.123456789123"
    assert event["side"] == "buy" and event["trade_id"] == "A"
    assert m["raw_sha256"] == sha256_file(raw)
    assert m["outputs"]["events.jsonl.gz"] == sha256_file(tmp_path / "out/events.jsonl.gz")
    assert m["status"] == "normalized"
    assert not m["training_ready"] and not m["capture_replay_compatible"]
    assert not m["full_day_coverage_proven"]


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-1", "0", "1e100000", "", "hello"])
def test_invalid_price(text):
    with pytest.raises(ArchiveError):
        decimal_text(text)


@pytest.mark.parametrize("text", ["nan", "-1", "0", "1704067200000000.1", "1e1000"])
def test_invalid_micro_timestamp(text):
    with pytest.raises(ArchiveError):
        timestamp_us(text)


def test_decimal_price_identity_and_timestamp_no_rounding():
    assert decimal_text("1e2") == decimal_text("100.000") == "100"
    assert decimal_text("-0.00", zero=True) == "0"
    with pytest.raises(ArchiveError):
        timestamp_us("1704067200.1234567", seconds=True)


@pytest.mark.parametrize("text", [BYBIT, BYBIT + row(side="bid"), BYBIT + row(amount="0"),
                                   BYBIT + row().replace("NEARUSDT", "BTCUSDT"),
                                   BYBIT + row(at="1704153600"),
                                   "timestamp,symbol,side\n1,NEARUSDT,Buy\n",
                                   BYBIT.replace("trdMatchID", "price") + row(),
                                   BYBIT + row().rstrip()+",extra\n",
                                   BYBIT + '1704067200,NEARUSDT,Buy,1,2,"unclosed'])
def test_failed_import_leaves_no_complete_output(tmp_path, text):
    raw = save(tmp_path, text)
    import csv
    with pytest.raises((ArchiveError, csv.Error)):
        import_archive(raw, spec(), tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".out-*"))
    assert raw.exists()


def test_truncated_gzip_cannot_be_accepted(tmp_path):
    raw = save(tmp_path, BYBIT + row())
    raw.write_bytes(raw.read_bytes()[:-6])
    with pytest.raises(EOFError):
        import_archive(raw, spec(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_no_overwrite_hash_check_and_input_unchanged(tmp_path):
    raw = save(tmp_path, BYBIT + row())
    digest = sha256_file(raw)
    with pytest.raises(ArchiveError):
        import_archive(raw, spec(), tmp_path / "wrong", expected_sha256="0"*64)
    import_archive(raw, spec(), tmp_path / "out", expected_sha256=digest)
    with pytest.raises(FileExistsError):
        import_archive(raw, spec(), tmp_path / "out")
    assert sha256_file(raw) == digest


@pytest.mark.parametrize("limits", [ReadLimits(max_rows=1), ReadLimits(max_text_bytes=80), ReadLimits(max_line_chars=10)])
def test_resource_caps_are_not_silent_truncation(tmp_path, limits):
    raw = save(tmp_path, BYBIT + row()+row(id="B"))
    with pytest.raises(ArchiveError):
        import_archive(raw, spec(), tmp_path / "out", limits=limits)
    assert not (tmp_path / "out").exists()


def test_trade_duplicate_retained_and_flagged_not_inflated_silently(tmp_path):
    raw = save(tmp_path, BYBIT + row()+row())
    m = import_archive(raw, spec(), tmp_path / "out")
    assert len(read_output(tmp_path / "out")) == 2
    assert m["counts"]["duplicate_ids_within_10000"] == 1
    assert m["quality_status"] == "requires_review"


def test_conflicting_trade_id_fails(tmp_path):
    raw = save(tmp_path, BYBIT + row()+row(price="4"))
    with pytest.raises(ArchiveError):
        import_archive(raw, spec(), tmp_path / "out")


def test_exchange_time_regression_not_sorted(tmp_path):
    raw = save(tmp_path, BYBIT + row(at="1704067201")+row(at="1704067200", id="B"))
    m = import_archive(raw, spec(), tmp_path / "out")
    events = read_output(tmp_path / "out")
    assert events[0]["exchange_time_us"] > events[1]["exchange_time_us"]
    assert m["quality_status"] == "requires_review"


def test_tardis_trades_preserve_vendor_clock_unknown_side_and_missing_id(tmp_path):
    raw = save(tmp_path, TRADES + f"bybit,NEARUSDT,{T},{T+10},,unknown,3,1\n")
    m = import_archive(raw, spec("tardis-trades"), tmp_path / "out")
    event = read_output(tmp_path / "out")[0]
    assert event["available_time_us"] == T+10 and event["trade_id"] is None
    assert event["side"] == "unknown" and m["counts"]["unknown_aggressor"] == 1


def test_l2_batch_boundary_uses_arrival_not_exchange(tmp_path):
    text = L2 + bookrow(snapshot=True) + bookrow(side="ask", price="101", snapshot=True)
    text += bookrow(local=20, exchange=5, amount="2") + bookrow(local=30, exchange=5, amount="3")
    text += bookrow(local=30, exchange=6, side="ask", price="101", amount="4")
    events = list(normalized_events(save(tmp_path, text), spec("tardis-l2"), ReadLimits()))
    assert len(events) == 3
    assert len(events[-1]["changes"]) == 2
    assert events[1]["exchange_time_us"] == T+5
    assert events[2]["exchange_time_us"] == T+6


def test_snapshot_reset_whole_batch_and_absolute_amount(tmp_path):
    text = L2+bookrow(local=1)  # buffered delta before snapshot
    text += bookrow(local=2, snapshot=True, price="100.0", amount="4")
    text += bookrow(local=3, snapshot=True, side="ask", price="101")
    text += bookrow(local=4, price="1e2", amount="2")
    text += bookrow(local=5, snapshot=True, price="98")
    text += bookrow(local=5, snapshot=True, side="ask", price="99")
    text += bookrow(local=6, price="98", amount="0")
    book = BookValidator()
    events = list(normalized_events(save(tmp_path,text), spec("tardis-l2"), ReadLimits()))
    assert book.apply(events[0]) == "uninitialized"
    assert book.apply(events[1]) == "two_sided_uncrossed"
    assert events[1]["available_time_us"] == T+3
    book.apply(events[2]); assert book.levels["bid"][Decimal("100")] == Decimal("2")
    book.apply(events[3]); assert Decimal("100") not in book.levels["bid"]
    assert book.best("bid") == Decimal("98")
    assert book.apply(events[4]) == "one_sided"


def test_crossing_within_message_not_published_as_complete_book(tmp_path):
    text = L2+bookrow(snapshot=True)+bookrow(snapshot=True,side="ask",price="101")
    text += bookrow(local=20,side="bid",price="102")+bookrow(local=20,side="ask",price="101",amount="0")
    text += bookrow(local=20,side="ask",price="103")
    m = import_archive(save(tmp_path,text), spec("tardis-l2"), tmp_path/"out")
    assert m["counts"]["two_sided_uncrossed"] == 2
    assert not m["counts"].get("crossed",0)


def test_observed_crossed_book_is_flagged_not_fixed(tmp_path):
    text = L2+bookrow(snapshot=True,price="102")+bookrow(snapshot=True,side="ask",price="101")
    m = import_archive(save(tmp_path,text), spec("tardis-l2"), tmp_path/"out")
    assert m["counts"]["crossed"] == 1 and m["quality_status"] == "requires_review"
    assert read_output(tmp_path/"out")[0]["changes"][0]["price"] == "102"


def test_no_snapshot_means_no_book(tmp_path):
    m = import_archive(save(tmp_path,L2+bookrow()), spec("tardis-l2"), tmp_path/"out")
    assert m["counts"]["rows_before_snapshot"] == 1
    assert m["quality_status"] == "requires_review"


@pytest.mark.parametrize("text", [L2+bookrow(local=20)+bookrow(local=10),
                                   L2+bookrow().replace("false", "1"),
                                   L2+bookrow().replace("bybit", "binance")])
def test_bad_l2_order_and_flags_fail_closed(tmp_path,text):
    with pytest.raises(ArchiveError):
        import_archive(save(tmp_path,text), spec("tardis-l2"), tmp_path/"out")


def test_limits_bound_batch_and_book(tmp_path):
    raw=save(tmp_path,L2+bookrow(snapshot=True)+bookrow(side="ask",price="101",snapshot=True))
    with pytest.raises(ArchiveError):
        list(normalized_events(raw,spec("tardis-l2"),ReadLimits(max_batch_rows=1)))
    book=BookValidator(max_levels_per_side=1)
    event=dict(is_snapshot=True,changes=[dict(side="bid",price="100",amount="1"),dict(side="bid",price="99",amount="1")])
    with pytest.raises(ArchiveError):
        book.apply(event)


def test_reproducible_output_hash(tmp_path):
    raw=save(tmp_path,BYBIT+row())
    one=import_archive(raw,spec(),tmp_path/"one")
    two=import_archive(raw,spec(),tmp_path/"two")
    assert one["outputs"]==two["outputs"]


def test_cli_plan_and_import(tmp_path):
    run=subprocess.run([sys.executable,"-m","scalp_bot.ml.history","plan","--provider","bybit-public-trades", "--symbol","NEARUSDT","--day",DAY],capture_output=True,text=True)
    assert run.returncode==0
    descriptor=tmp_path/"source.json";descriptor.write_text(run.stdout,encoding="utf-8")
    raw=save(tmp_path,BYBIT+row())
    done=subprocess.run([sys.executable,"-m","scalp_bot.ml.history","import","--source",str(descriptor),"--input",str(raw),"--output",str(tmp_path/"out")],capture_output=True,text=True)
    assert done.returncode==0,done.stderr
    assert not json.loads(done.stdout)["training_ready"]


def test_existing_runtime_is_still_unmodified_by_new_package():
    from scalp_bot.ml import MODEL_TRAINED, RUNTIME_CONNECTED
    assert not MODEL_TRAINED and not RUNTIME_CONNECTED


def test_download_rejects_paid_day_before_network(tmp_path):
    from scalp_bot.ml.history.samples import download_sample
    with pytest.raises(ValueError, match="paid"):
        download_sample(replace(spec("tardis-trades"),day="2024-01-02"),tmp_path/"a.gz")


def test_download_is_bounded_and_no_redirect(tmp_path,monkeypatch):
    import io
    from scalp_bot.ml.history import samples
    class Response(io.BytesIO):
        status=200
        headers={"Content-Length":"12"}
    class Opener:
        def open(self,*a,**kw): return Response(b"abcdefghijkl")
    monkeypatch.setattr(samples,"build_opener",lambda *a:Opener())
    with pytest.raises(ValueError,match="byte budget"):
        samples.download_sample(spec(),tmp_path/"too-big",max_bytes=10)
    assert not (tmp_path/"too-big").exists()
    result=samples.download_sample(spec(),tmp_path/"raw",max_bytes=20)
    assert result["bytes"]==12 and result["sha256"]==sha256_file(tmp_path/"raw")
    assert samples.NoRedirect().redirect_request(None,None,302,None,None,"https://other.test") is None


def test_tardis_exchange_timestamp_regression_allowed_without_arrival_sort(tmp_path):
    text=TRADES+f"bybit,NEARUSDT,{T+50},{T+100},A,buy,3,1\nbybit,NEARUSDT,{T},{T+200},B,sell,3,1\n"
    m=import_archive(save(tmp_path,text),spec("tardis-trades"),tmp_path/"out")
    assert m["counts"]["exchange_timestamp_regressions"]==1
    assert m["quality_status"]=="structural_checks_passed_only"


def test_input_mutation_blocks_manifest(tmp_path,monkeypatch):
    from scalp_bot.ml.history import importer
    path=save(tmp_path,BYBIT+row())
    original=importer.normalized_events
    def changed(*a):
        yield from original(*a)
        path.write_bytes(gzip.compress((BYBIT+row(id="DIFFERENT")).encode()))
    monkeypatch.setattr(importer,"normalized_events",changed)
    with pytest.raises(ArchiveError,match="changed"):
        importer.import_archive(path,spec(),tmp_path/"out")
    assert not (tmp_path/"out").exists()


@pytest.mark.parametrize("changes",[{"max_rows":0},{"max_batch_rows":True},{"max_text_bytes":-1}])
def test_read_limits_validate_types(changes):
    with pytest.raises(ValueError):
        ReadLimits(**changes)
