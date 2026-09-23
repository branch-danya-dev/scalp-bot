from __future__ import annotations

import os
import subprocess
import sys


def clean_test_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("SCALP_")
    }
    # Settings normally supports a developer-local .env. Unit tests must not:
    # their contract is the checked-in Python defaults plus explicit test args.
    env["SCALP_DISABLE_DOTENV"] = "1"
    return env


def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        env=clean_test_environment(),
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
