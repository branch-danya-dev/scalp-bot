"""Receipts for defects confirmed on the exact PR61 starting source."""
import pytest

from scalp_bot.ml.prepared_learning import digest, verify_dataset_evidence
from scalp_bot.ml.prepared_dataset import purged_walk_forward


def test_verified_hashes_cannot_promote_one_prepared_observation():
    rows = [dict(capture_id="single", identity="one")]
    evidence = dict(datasetHash=digest(rows), trainingReady=True, captures={
        "single": dict(primaryIntegrity="MET", labelReplay="MET",
                       sourceHash="a"*64, configHash="b"*64, runtimeHash="c"*64)})
    with pytest.raises(ValueError, match="promotion gate"):
        verify_dataset_evidence(rows, evidence)


def test_whole_episode_purge_includes_future_embargo_observations():
    rows = [dict(available_wall_ms=1, label_end_wall_ms=5, symbol="A", episode="shared"),
            dict(available_wall_ms=101, label_end_wall_ms=105, symbol="A", episode="shared"),
            dict(available_wall_ms=120, label_end_wall_ms=125, symbol="A", episode="other")]
    result = purged_walk_forward(rows, [(0, 100, 200)], embargo_ms=10)[0]
    assert 0 not in result["train"], "embargoed future episode still leaks into fitting"
    assert result["validation"] == [2]
