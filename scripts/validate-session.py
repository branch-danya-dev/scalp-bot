"""Read-only session validation; output is written only with explicit --output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp_bot.session_validation import validate_session


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate recorded research-run integrity, not profitability.")
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--future-tolerance-ms", type=float, default=0.0)
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.session.resolve():
        parser.error("output must not overwrite the input session")
    try:
        report = validate_session(args.session, future_tolerance_ms=args.future_tolerance_ms)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        # Refuse overwrite: validation artifacts should not silently replace evidence.
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
        print(f"{report['status']}: {args.output}")
    else:
        print(rendered, end="")
    return {"checks_passed": 0, "rejected": 1, "incomplete": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
