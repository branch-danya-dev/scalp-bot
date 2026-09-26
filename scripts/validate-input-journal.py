"""Validate experimental input capture, independently of session PnL integrity."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scalp_bot.input_journal import validate_input_journal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.session.resolve():
        parser.error("output must not overwrite input")
    try:
        result = validate_input_journal(args.session)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf8") as f:
                f.write(rendered)
            print(f"{result['structuralStatus']}; parityReady=false: {args.output}")
        else:
            print(rendered, end="")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    # Even an intact partial journal is not a successful parity preflight.
    return 1 if result['structuralStatus'] == 'rejected' else 2


if __name__ == "__main__":
    raise SystemExit(main())
