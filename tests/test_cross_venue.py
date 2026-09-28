from dataclasses import replace
import pytest
from scalp_bot.cross_venue import VenueEvent, CrossVenueRuntime, alignment
from scalp_bot.cross_venue_public import decode, instruments
from scalp_bot.research_journal import ResearchJournal, read_research


def quote(venue="binance", ms=0, price=100, **kw):
    return VenueEvent("capture", venue, "AAAUSDT", 0, "quote", 10000+ms, 10000+ms,
        ms*1_000_000, ms*1_000_000, ms+1, price-.01, price+.01, 1, 1, units_verified=True, **kw)


def test_causal_windows_asynchronous_arrival_missing_stale_and_clock_domains():
    runtime = CrossVenueRuntime("capture")
    for ms in range(0, 1100, 100):
        runtime.ingest(quote(ms=ms, price=100+ms/10000))
    snapshot = runtime.snapshot("AAAUSDT", 1_000_000_000)
    assert snapshot["venues"]["binance"]["returnsBps"]["500"] > 0
    assert alignment(snapshot, "long") == "unavailable"
    assert runtime.snapshot("AAAUSDT", 3_000_000_000)["venues"]["binance"]["reason"] == "stale"
    with pytest.raises(ValueError, match="clock domains"):
        runtime.ingest(replace(quote(), capture_id="other"))
    assert runtime.snapshot("AAAUSDT", 0)["venues"]["binance"]["returnsBps"]["500"] is None


def test_reconnect_gap_out_of_order_and_skew_invalidate_windows():
    runtime = CrossVenueRuntime("capture")
    runtime.ingest(quote(ms=1000))
    assert runtime.ingest(quote(ms=999)) == "out_of_order"
    runtime.gap("binance", "AAAUSDT", 1, "disconnected")
    assert runtime.ingest(quote(ms=1100)) == "old_epoch"
    runtime.ingest(replace(quote(ms=1200), epoch=1))
    assert runtime.snapshot("AAAUSDT", 1_200_000_000)["venues"]["binance"]["returnsBps"]["500"] is None
    assert runtime.ingest(replace(quote(ms=1300), epoch=1, receipt_wall_ms=20000)) == "clock_quality"
    assert not runtime.snapshot("AAAUSDT", 1_300_000_000)["venues"]["binance"]["available"]


def test_okx_contract_quantities_and_aggressor_are_explicit():
    spec = dict(instrument="BTC-USDT-SWAP", multiplier=.01)
    events = decode("okx", {"arg":{"instId":spec["instrument"],"channel":"trades"},
        "data":[{"tradeId":"1","ts":"1000","px":"50000","sz":"10","side":"sell"}]},
        capture_id="c", symbol="BTCUSDT", epoch=0, spec=spec, receipt_wall_ms=1000, receipt_ns=1, processing_ns=2)
    assert events[0].quantity == .1 and events[0].side == "sell"
    assert not instruments("okx", {"data":[{"instType":"SWAP","ctType":"inverse"}]})


def test_research_writer_detaches_and_hashes_and_requires_footer(tmp_path):
    path = tmp_path/"research.gz"
    journal = ResearchJournal(path, "capture", {"source":"hash"})
    row = {"symbol":"AAA", "values":[1]}
    journal.append("test", row)
    row["values"].append(2)
    journal.close()
    assert journal.health()["complete"]
    assert list(read_research(path))[1]["body"]["values"] == [1]
