from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scalp_bot.config import Settings
from scalp_bot.manifest_validation import check_manifest, fingerprint
from scalp_bot.run_manifest import build_run_manifest
from scalp_bot.session_validation import validate_session


def example():
    hashes = {"scalp_bot/example.py": "a" * 64}
    return build_run_manifest(Settings(_env_file=None), {"level_breakout": True},
                              code={"fileHashes": hashes, "sourceSha256": fingerprint(hashes)},
                              policy={"mode": "off"})


def rehash(m):
    m["manifestSha256"] = fingerprint({k: v for k, v in m.items() if k not in {"manifestId", "manifestSha256"}})


def test_current_manifest_checks_and_runtime_metadata():
    m = example()
    result, issues = check_manifest(m)
    assert not issues
    assert result["status"] == "recorded_hashes_checked"
    assert m["runtime"]["python"]
    assert any(p["name"].lower() == "msgspec" for p in m["runtime"]["packages"])


@pytest.mark.parametrize("section,key,value,code", [
    ("config", "risk_fraction", 0.99, "manifest_config_hash_mismatch"),
    ("code", "sourceSha256", "b" * 64, "manifest_source_hash_mismatch"),
    ("runtime", "python", "0.0.0", "manifest_runtime_hash_mismatch"),
])
def test_inner_hashes_are_checked_even_if_outer_is_recomputed(section, key, value, code):
    m = example()
    m[section][key] = value
    rehash(m)
    result, issues = check_manifest(m)
    assert result["status"] == "invalid"
    assert (code, "error") in issues


def test_outer_hash_detects_changed_strategy():
    m = example()
    m["strategies"]["enabled"] = []
    assert ("manifest_hash_mismatch", "error") in check_manifest(m)[1]


def test_old_manifest_has_explicit_missing_runtime_and_missing_manifest_is_unknown():
    assert check_manifest(None)[0]["status"] == "unknown"
    m = example()
    m["manifestVersion"] = 1
    del m["runtime"], m["runtimeSha256"]
    rehash(m)
    assert check_manifest(m)[0]["status"] == "incomplete"


@pytest.mark.parametrize("bad", [[], "private-canary", {"manifestId": "private-canary"}, {"config": []}])
def test_malformed_manifest_returns_safe_findings(bad):
    result, issues = check_manifest(bad)
    assert issues
    assert "private-canary" not in json.dumps([result, issues])


def test_missing_public_fields_and_secret_in_config_never_pass():
    m = example()
    del m["config"]["risk_fraction"]
    m["config"]["bybit_api_secret"] = "PRIVATE_CANARY"
    m["configSha256"] = fingerprint(m["config"])
    rehash(m)
    result, issues = check_manifest(m)
    assert ("manifest_contains_secret_fields", "error") in issues
    assert ("manifest_config_fields_incomplete", "incomplete") in issues
    assert "PRIVATE_CANARY" not in json.dumps([result, issues])


def rows(m, start=10):
    return [
        {"event": "bot_started", "ts": start, "payload": {"manifest": m}},
        {"event": "research_frame", "ts": start + 1, "payload": {"formingCandle": {
            "lastTradeTsMs": start * 1000, "observedAtMs": (start + 1) * 1000}}},
        {"event": "run_summary", "ts": start + 2, "payload": {
            "manifestId": m["manifestId"], "manifestSha256": m["manifestSha256"],
            "reason": "duration_elapsed", "realizedPnl": 0, "closedTrades": 0,
            "recorderHealth": {"droppedRows": 0}}},
    ]


def validate(tmp_path, events):
    p = tmp_path / "session.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    return validate_session(p)


def codes(report):
    return {f["code"] for r in report["runs"] for f in r["findings"]}


def test_summary_links_and_duplicate_run_ids(tmp_path):
    m = example()
    events = rows(m)
    assert validate(tmp_path, events)["status"] == "checks_passed"
    corrupt = deepcopy(events)
    corrupt[-1]["payload"]["manifestId"] = "0" * 32
    assert "summary_manifest_id_mismatch" in codes(validate(tmp_path, corrupt))
    corrupt[-1]["payload"]["manifestSha256"] = "0" * 64
    assert "summary_manifest_hash_mismatch" in codes(validate(tmp_path, corrupt))
    assert "reused_manifest_id" in codes(validate(tmp_path, events + rows(m, 20)))


def test_toggle_link_and_legacy_summary_reference(tmp_path):
    m = example()
    events = rows(m)
    events.insert(2, {"event": "strategy_toggle", "ts": 11.5, "payload": {"manifestId": "0" * 32}})
    assert "strategy_toggle_manifest_mismatch" in codes(validate(tmp_path, events))
    del events[0]["payload"]["manifest"]
    assert "summary_manifest_without_start_manifest" in codes(validate(tmp_path, events))


def test_offline_validator_import_never_loads_runtime_settings(tmp_path):
    (tmp_path / ".env").write_text("SCALP_RISK_FRACTION=not-a-number\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCALP_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c",
                             "import sys; import scalp_bot.session_validation; "
                             "assert 'scalp_bot.config' not in sys.modules"],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
