import pytest
pytest.importorskip("sklearn")
from scalp_bot.ml.prepared_learning import eligible, splits, _fit_baseline as fit, predict, report, evaluate


def rows():
    return [dict(identity=str(i), capture_id="c"+str(i//100), symbol="AAA" if i%2 else "BBB",
        episode=str(i), available_wall_ms=i*2000, label_end_wall_ms=i*2000+1000,
        source=dict(clock_domain="capture:c"+str(i//100)), trainingReady=True,
        censor_reason=None, features=[float(i%7), None], side="long", segment={},
        target_before_stop=bool(i%3), realized_net_r=float(i%3)-1) for i in range(600)]


def test_global_clock_domains_and_test_captures_cannot_be_mixed():
    data = rows()
    data[0]["source"]["clock_domain"] = "other"
    with pytest.raises(ValueError, match="clock domain"): eligible(data)
    with pytest.raises(ValueError, match="test"):
        evaluate(rows(), dict(untouched_test_captures=["c0"]))


def test_calibration_is_separate_and_episodes_are_purged():
    data = rows()
    split = splits(data, [0, 400000, 800000, 1200000], 60000, ["AAA"])
    assert set(split["train"]).isdisjoint(split["calibration"])
    assert max(data[i]["label_end_wall_ms"] for i in split["train"]) < 340000
    assert all(data[i]["symbol"] != "AAA" for i in split["train"]+split["calibration"])
    assert min(data[i]["available_wall_ms"] for i in split["validation"]) >= 860000
    data[split["train"][0]]["episode"] = data[split["validation"][0]]["episode"]
    new = splits(data, [0, 400000, 800000, 1200000], 60000, ["AAA"])
    assert split["train"][0] not in new["train"]


@pytest.mark.parametrize("kind", ["logistic", "catboost"])
def test_baselines_rank_economics_without_portfolio_sum(kind):
    if kind == "catboost": pytest.importorskip("catboost")
    data = rows()
    split = splits(data, [0, 400000, 800000, 1200000], 60000)
    model = fit(data, split, kind)
    valid = [data[i] for i in split["validation"]]
    result = report(valid, *predict(model, valid))
    assert result["samples"] == len(valid) and result["portfolioPnl"] is None
    assert 0 <= result["brier"] <= 1 and "0.25" in result["economicRanking"]
