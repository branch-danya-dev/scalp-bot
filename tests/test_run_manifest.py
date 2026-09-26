import json

import pytest
from pydantic import SecretStr

from scalp_bot.config import Settings
from scalp_bot.run_manifest import (
    PUBLIC_CONFIG_FIELDS, SECRET_CONFIG_FIELDS, build_run_manifest, code_provenance,
)


def manifest(config=None, strategies=None):
    return build_run_manifest(
        config or Settings(_env_file=None),
        strategies or {"level_breakout": True, "orderbook_density": True},
        code={"sourceSha256": "example"}, policy={"mode": "off"},
    )


def test_all_current_settings_explicitly_classified():
    assert PUBLIC_CONFIG_FIELDS | SECRET_CONFIG_FIELDS == set(Settings.model_fields)
    assert not PUBLIC_CONFIG_FIELDS & SECRET_CONFIG_FIELDS


def test_secrets_never_serialized_or_hashed():
    a = Settings(_env_file=None, bybit_api_key="PRIVATE_KEY_A", bybit_api_secret="PRIVATE_SECRET_A")
    b = Settings(_env_file=None, bybit_api_key="PRIVATE_KEY_B", bybit_api_secret="PRIVATE_SECRET_B")
    first, second = manifest(a), manifest(b)
    assert first["manifestSha256"] == second["manifestSha256"]
    assert "PRIVATE_" not in json.dumps(first)
    assert "bybit_api_key" not in first["config"]


def test_future_secret_field_fails_closed_without_exposing_value():
    class FutureSettings(Settings):
        service_token: str = "CANARY_FUTURE_SECRET"
    result = manifest(FutureSettings(_env_file=None))
    assert result["unclassifiedConfigFields"] == ["service_token"]
    assert "CANARY_FUTURE_SECRET" not in json.dumps(result)


def test_public_field_changed_to_secret_type_is_rejected():
    config = Settings(_env_file=None)
    config.run_label = SecretStr("CANARY")
    with pytest.raises(ValueError, match="non-public type"):
        manifest(config)


def test_run_identity_hash_and_snapshot_are_independent():
    config = Settings(_env_file=None)
    strategies = {"level_breakout": True, "orderbook_density": True}
    first = manifest(config, strategies)
    second = manifest(config, strategies)
    assert first["manifestId"] != second["manifestId"]
    assert first["manifestSha256"] == second["manifestSha256"]
    config.risk_fraction = 0.01
    strategies["level_breakout"] = False
    third = manifest(config, strategies)
    assert first["configSha256"] != third["configSha256"]
    assert first["strategies"]["tradeable"] == ["level_breakout"]
    assert third["strategies"]["tradeable"] == []


def test_source_hash_includes_untracked_code_and_ignores_dotenv(tmp_path):
    package = tmp_path / "scalp_bot"
    package.mkdir()
    source = package / "example.py"
    source.write_text("x = 1\n")
    (tmp_path / ".env").write_text("CANARY_SECRET")
    first = code_provenance(tmp_path)
    source.write_text("x = 2\n")
    second = code_provenance(tmp_path)
    assert first["sourceSha256"] != second["sourceSha256"]
    assert first["gitHead"] is None
    assert "CANARY_SECRET" not in json.dumps(first)
    assert set(first["fileHashes"]) == {"scalp_bot/example.py"}
