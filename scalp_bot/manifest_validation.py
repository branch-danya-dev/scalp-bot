"""Offline manifest checks; does not import runtime Settings or read dotenv."""
from __future__ import annotations

import hashlib
import json
import math
import re
from .manifest_schema import PUBLIC_CONFIG_FIELDS, SECRET_CONFIG_FIELDS, LEGACY_PUBLIC_CONFIG_FIELDS, V3_PUBLIC_CONFIG_FIELDS


def fingerprint(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def valid_digest(value: object, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def check_manifest(value: object) -> tuple[dict, list[tuple[str, str]]]:
    """Return only safe identifiers and findings, never supplied config values."""
    result = {"status": "unknown", "manifestId": None, "manifestSha256": None}
    findings: list[tuple[str, str]] = []
    if value is None:
        return result, findings
    if not isinstance(value, dict):
        return {**result, "status": "invalid"}, [("invalid_manifest", "error")]
    if valid_digest(value.get("manifestId"), 32):
        result["manifestId"] = value["manifestId"]
    else:
        findings.append(("invalid_manifest_id", "error"))
    if valid_digest(value.get("manifestSha256")):
        result["manifestSha256"] = value["manifestSha256"]
    if type(value.get("manifestVersion")) is not int or value["manifestVersion"] not in (1, 2, 3, 4):
        findings.append(("unsupported_manifest_version", "incomplete"))
    try:
        body = {key: item for key, item in value.items() if key not in {"manifestId", "manifestSha256"}}
        if fingerprint(body) != result["manifestSha256"]:
            findings.append(("manifest_hash_mismatch", "error"))
        config = value.get("config")
        if not isinstance(config, dict) or not config:
            findings.append(("manifest_config_unavailable", "incomplete"))
        else:
            if any(type(v) not in (str, bool, int, float, type(None)) or
                   (type(v) is float and not math.isfinite(v)) for v in config.values()):
                findings.append(("invalid_manifest_config", "error"))
            if SECRET_CONFIG_FIELDS & config.keys():
                findings.append(("manifest_contains_secret_fields", "error"))
            expected = (PUBLIC_CONFIG_FIELDS if value.get("manifestVersion") == 4 else
                        V3_PUBLIC_CONFIG_FIELDS if value.get("manifestVersion") == 3 else LEGACY_PUBLIC_CONFIG_FIELDS)
            if set(config) != expected:
                findings.append(("manifest_config_fields_incomplete", "incomplete"))
            if fingerprint(config) != value.get("configSha256"):
                findings.append(("manifest_config_hash_mismatch", "error"))
        if value.get("unclassifiedConfigFields") != []:
            findings.append(("manifest_config_coverage_unknown", "incomplete"))
        code = value.get("code")
        if not isinstance(code, dict) or not isinstance(code.get("fileHashes"), dict) or not code["fileHashes"]:
            findings.append(("manifest_source_unavailable", "incomplete"))
        elif any(not valid_digest(v) for v in code["fileHashes"].values()) or fingerprint(code["fileHashes"]) != code.get("sourceSha256"):
            findings.append(("manifest_source_hash_mismatch", "error"))
        runtime = value.get("runtime")
        if not isinstance(runtime, dict) or not isinstance(runtime.get("packages"), list) or not runtime.get("python"):
            findings.append(("manifest_runtime_unavailable", "incomplete"))
        elif fingerprint(runtime) != value.get("runtimeSha256"):
            findings.append(("manifest_runtime_hash_mismatch", "error"))
        strategies = value.get("strategies")
        if not isinstance(strategies, dict) or any(
            not isinstance(strategies.get(k), list) or
            any(not isinstance(v, str) for v in strategies[k])
            for k in ("enabled", "evidenceOnly", "tradeable")
        ):
            findings.append(("invalid_manifest_strategies", "error"))
        else:
            enabled, evidence, tradeable = (set(strategies[k]) for k in ("enabled", "evidenceOnly", "tradeable"))
            if evidence & tradeable or enabled != evidence | tradeable or any(
                len(strategies[k]) != len(set(strategies[k])) for k in ("enabled", "evidenceOnly", "tradeable")
            ):
                findings.append(("invalid_manifest_strategies", "error"))
        if not value.get("recordSchemaVersion") or not value.get("executionModelVersion"):
            findings.append(("manifest_model_version_unavailable", "incomplete"))
    except (TypeError, ValueError, OverflowError):
        findings.append(("invalid_manifest", "error"))
    result["status"] = "invalid" if any(s == "error" for _, s in findings) else "incomplete" if findings else "recorded_hashes_checked"
    return result, findings
