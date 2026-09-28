from copy import deepcopy

import pytest

from scalp_bot.manifest_validation import fingerprint
from scalp_bot.native_index import IndexedNativeTape
from scalp_bot.native_v5 import NativeTapeError, read_native_tape, write_sealed_rows
from test_native_v5 import make_tape, freeze


def test_index_validates_complete_population_beyond_fixture_cap(tmp_path):
    tape = make_tape(tmp_path/"raw.gz")
    with pytest.raises(NativeTapeError, match="population bound"):
        read_native_tape(tmp_path/"raw.gz", max_events=1)
    with IndexedNativeTape(tmp_path/"raw.gz", tmp_path/"index.sqlite", expected_provenance=freeze()) as indexed:
        assert indexed.counts == tape.counts
        assert indexed.population_hash == tape.population_hash
        assert list(indexed.events) == tape.events
        assert list(indexed.task_events("market-7")) == tape.events
        assert indexed.receipt["completeChain"] and not indexed.receipt["productionCoverage"]


@pytest.mark.parametrize("defect", ["clock_owner", "unfinished", "scope", "inventory", "provenance", "artifact"])
def test_index_rejects_resealed_corruption_and_incomplete_proof(tmp_path, defect):
    tape = make_tape(tmp_path/"raw.gz")
    rows = deepcopy(tape.events)
    kwargs = dict(expected_provenance=freeze())
    if defect == "clock_owner": rows[2]["module_id"] = "maker"
    if defect == "unfinished": rows.pop()
    if defect == "scope":
        rows[0]["data"]["operation"] = "runtime"
        rows[-2]["data"] = dict(name="scope_begin", value={"name":"unclosed"})
    if defect == "inventory": kwargs.update(external_inventory=[], expected_inventory_sha256="f"*64)
    if defect == "provenance": kwargs["expected_provenance"] = dict(freeze(), sourceSha256="e"*64)
    if defect == "artifact": kwargs["artifact_hashes"] = {"model":dict(path=str(tmp_path/"raw.gz"), sha256="f"*64)}
    write_sealed_rows(tmp_path/"bad.gz", tape.header, rows)
    with pytest.raises(NativeTapeError):
        IndexedNativeTape(tmp_path/"bad.gz", tmp_path/"index.sqlite", **kwargs)
