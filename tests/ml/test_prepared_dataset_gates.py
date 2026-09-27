import pytest
from scalp_bot.ml.prepared_learning import train_frozen


def test_training_rejects_unverified_primary_capture_before_fitting(tmp_path):
    with pytest.raises(ValueError, match="dataset evidence"):
        train_frozen([], dict(untouched_test_captures=["test"], final_window=[0,100,200,300],
            embargo_ms=60_000), tmp_path/"model", kind="logistic")


def test_calibration_bins_count_boundary_probability_once():
    np = pytest.importorskip("numpy")
    from scalp_bot.ml.prepared_learning import report
    rows = [dict(realized_net_r=1, target_before_stop=True, symbol="A", capture_id="c", segment={})]
    result = report(rows, np.array([.1]), np.array([1.]))
    assert sum(b["samples"] for b in result["calibration"]) == 1
