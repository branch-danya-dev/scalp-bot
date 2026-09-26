"""Versioned, secret-free provenance for paper runs. No environment is serialized."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.metadata import distributions
import platform
from pathlib import Path
import subprocess
from uuid import uuid4

from .config import Settings
from .manifest_validation import fingerprint

from .manifest_schema import PUBLIC_CONFIG_FIELDS, SECRET_CONFIG_FIELDS


def runtime_provenance() -> dict:
    # Only public name/version metadata; never serialize installation paths or URLs.
    # Uvicorn, pytest and script entry points place the editable repository on
    # sys.path a different number of times. Repeated identical metadata is not
    # a dependency change. Keep distinct versions visible for strict replay.
    unique = {(d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"]}
    packages = [{"name": name, "version": version} for name, version in
                sorted(unique, key=lambda item: (item[0].lower(), item[1], item[0]))]
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "machine": platform.machine(), "packages": packages}


def code_provenance(root: Path) -> dict:
    """Identify source files on disk; never read dotenv, raw data or Git diffs."""
    files = sorted((root / "scalp_bot").rglob("*.py"))
    files += [p for p in (root / "pyproject.toml", root / "requirements.txt") if p.is_file()]
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    result = {"sourceSha256": fingerprint(hashes), "fileHashes": hashes,
              "gitHead": None, "trackedDirty": None,
              "scope": "source_on_disk_at_run_start_not_loaded_bytecode"}
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, check=True, timeout=3).stdout.strip()
    try:
        result["gitHead"] = git("rev-parse", "HEAD")
        result["trackedDirty"] = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        result["gitStatus"] = "unavailable"
    return result


def build_run_manifest(config: Settings, strategies: dict[str, bool], *,
                       code: dict, policy: dict) -> dict:
    values = {name: getattr(config, name) for name in sorted(PUBLIC_CONFIG_FIELDS)}
    # Fail closed if a reviewed public field is changed to a secret/nonprimitive type.
    if any(type(value) not in (str, int, float, bool, type(None)) for value in values.values()):
        raise ValueError("non-public type in run manifest configuration")
    config_hash = fingerprint(values)
    enabled = sorted(key for key, value in strategies.items() if value)
    runtime = runtime_provenance()
    body = {"manifestVersion": 4, "recordSchemaVersion": "jsonl-clock-v2",
            "executionModelVersion": "paper-v1", "config": values,
            "runtime": runtime, "runtimeSha256": fingerprint(runtime),
            "configSha256": config_hash, "code": deepcopy(code),
            "strategies": {"enabled": enabled,
                           "evidenceOnly": [key for key in enabled if key == "orderbook_density"],
                           "tradeable": [key for key in enabled if key != "orderbook_density"]},
            "researchPolicy": deepcopy(policy),
            "excludedConfigFields": sorted(SECRET_CONFIG_FIELDS),
            "unclassifiedConfigFields": sorted(set(type(config).model_fields) - PUBLIC_CONFIG_FIELDS - SECRET_CONFIG_FIELDS)}
    body["manifestSha256"] = fingerprint(body)
    body["manifestId"] = uuid4().hex
    return body
