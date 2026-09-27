from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scalp_bot.ml.arbitration import ProposalSource, SymbolPhase, select_proposal
from scalp_bot.ml.contracts import FeatureSnapshot, ImpulseForecast, SnapshotRef, forecast_rejections


def source():
    return SnapshotRef("capture-A", "BTCUSDT", 1, 10, 100000, 100, "boot-A", "features-v1")


def forecast():
    return ImpulseForecast(source(), "model-v1", "plan-v1", "short", 30000, 120, 500, .6, .3, .1)


def check(value, current=None, **kw):
    args = dict(now_mono_ns=150, max_data_age_ns=200, model_version="model-v1", plan_policy_version="plan-v1")
    args.update(kw)
    return forecast_rejections(value, current or replace(source(), source_sequence=11, available_mono_ns=140), **args)


def test_valid_message_is_not_an_order():
    assert check(forecast()) == ()
    assert not hasattr(forecast(), "quantity")
    with pytest.raises(FrozenInstanceError):
        forecast().side = "long"


@pytest.mark.parametrize("field,value,reason", [
    ("capture_id", "B", "capture_id_mismatch"),
    ("symbol", "ETHUSDT", "symbol_mismatch"),
    ("selection_epoch", 2, "selection_epoch_mismatch"),
    ("clock_domain", "boot-B", "clock_domain_mismatch"),
    ("feature_schema", "features-v2", "feature_schema_mismatch"),
    ("source_sequence", 9, "future_source_sequence"),
    ("available_mono_ns", 90, "source_newer_than_current"),
])
def test_rejects_wrong_source(field, value, reason):
    assert reason in check(forecast(), replace(source(), **{field: value}))


@pytest.mark.parametrize("kwargs,reason", [
    ({"model_version": "model-v2"}, "model_version_mismatch"),
    ({"plan_policy_version": "plan-v2"}, "plan_policy_version_mismatch"),
    ({"now_mono_ns": 500}, "forecast_expired"),
    ({"now_mono_ns": 300}, "source_data_stale"),
    ({"now_mono_ns": 110}, "future_timestamp"),
])
def test_versions_and_freshness(kwargs, reason):
    assert reason in check(forecast(), **kwargs)


def test_recently_produced_prediction_cannot_refresh_old_data():
    assert "source_data_stale" in check(replace(forecast(), produced_mono_ns=290), now_mono_ns=300)


@pytest.mark.parametrize("probabilities", [(float("nan"), .3, .1), (float("inf"), 0, 0), (-.1, .9, .2), (.2, .2, .2), (True, 0, 0)])
def test_invalid_probabilities(probabilities):
    with pytest.raises(ValueError):
        replace(forecast(), p_target_first=probabilities[0], p_stop_first=probabilities[1], p_timeout=probabilities[2])


@pytest.mark.parametrize("changes", [{"produced_mono_ns": 99}, {"expires_mono_ns": 120}, {"horizon_ms": 0}, {"side": "up"}, {"horizon_ms": True}])
def test_invalid_forecast(changes):
    with pytest.raises(ValueError):
        replace(forecast(), **changes)


@pytest.mark.parametrize("changes", [{"source_sequence": -1}, {"selection_epoch": True}, {"available_mono_ns": 1.5}, {"symbol": ""}])
def test_invalid_source(changes):
    with pytest.raises(ValueError):
        replace(source(), **changes)


@pytest.mark.parametrize("names,values", [((), ()), (("a",), ()), (("a", "a"), (1., 2.)), (("a",), (float("nan"),)), (("a",), (True,)), (["a"], [1.])])
def test_invalid_features(names, values):
    with pytest.raises(ValueError):
        FeatureSnapshot(source(), names, values)


def test_missing_feature_is_not_zero():
    frame = FeatureSnapshot(source(), ("flow", "wall"), (0., None))
    assert frame.values == (0., None)


@pytest.mark.parametrize("phase", list(SymbolPhase))
@pytest.mark.parametrize("rule_ready,ml_ready", [(False, False), (False, True), (True, False), (True, True)])
def test_priority_table(phase, rule_ready, ml_ready):
    available = phase in {SymbolPhase.FREE, SymbolPhase.OBSERVING, SymbolPhase.PREPARING, SymbolPhase.READY}
    expected = ProposalSource.NONE
    if available and rule_ready:
        expected = ProposalSource.RULE
    elif available and phase != SymbolPhase.READY and ml_ready:
        expected = ProposalSource.ML
    assert select_proposal(phase, rule_ready=rule_ready, ml_ready=ml_ready) == expected


def test_rejects_unknown_phase():
    with pytest.raises(ValueError):
        select_proposal("free", rule_ready=False, ml_ready=True)


def test_status_does_not_train_or_connect():
    output = subprocess.check_output([sys.executable, "-m", "scalp_bot.ml"], text=True)
    status = json.loads(output)
    assert status["stage"] == "M2_TECHNICAL_M3_OFFLINE"
    assert not status["bundled_model_trained"] and not status["runtime_connected"] and not status["order_authority"]


def test_existing_runtime_does_not_import_ml():
    root = Path(__file__).resolve().parents[2] / "scalp_bot"
    for path in root.rglob("*.py"):
        if not {"ml", "demo_paper"}.intersection(path.relative_to(root).parts) and path.name not in {"offline_study.py", "offline_benchmark.py"}:
            text = path.read_text(encoding="utf-8")
            assert "scalp_bot.ml" not in text and "from .ml" not in text


def test_ordinary_engine_import_graph_excludes_offline_learning_entrypoints():
    output=subprocess.check_output([sys.executable,'-c',
        "import sys; import scalp_bot.engine; assert not any(n == 'scalp_bot.ml' or n.startswith('scalp_bot.ml.') for n in sys.modules); assert 'scalp_bot.offline_study' not in sys.modules; assert 'scalp_bot.offline_benchmark' not in sys.modules; print('ordinary engine isolated')"],text=True)
    assert output.strip()=='ordinary engine isolated'


def test_normal_engine_does_not_load_opt_in_research_or_ml():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c",
        "import sys; import scalp_bot.engine; "
        "assert not any(k.startswith(('scalp_bot.ml', 'scalp_bot.demo_paper')) for k in sys.modules)"],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
