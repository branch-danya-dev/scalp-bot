"""Offline plan/import commands. No download, training or trading is implicit."""
import argparse
import csv
import zlib
import json
from pathlib import Path
import sys

from .importer import import_archive
from .sources import ArchiveSpec, PROVIDERS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="print a source descriptor; no network")
    plan.add_argument("--provider", choices=PROVIDERS, required=True)
    plan.add_argument("--symbol", required=True)
    plan.add_argument("--day", required=True)
    plan.add_argument("--purpose", default="development", choices=("development", "format_smoke_only", "reserved_holdout"))
    imp = commands.add_parser("import", help="normalize one complete local CSV/gzip")
    imp.add_argument("--source", type=Path, required=True)
    imp.add_argument("--input", type=Path, required=True)
    imp.add_argument("--output", type=Path, required=True)
    imp.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = ArchiveSpec(args.provider, args.symbol, args.day, args.purpose).public()
        else:
            spec = ArchiveSpec.from_public(json.loads(args.source.read_text(encoding="utf-8-sig")))
            result = import_archive(args.input, spec, args.output, expected_sha256=args.expected_sha256)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, OSError, EOFError, KeyError, csv.Error, zlib.error) as exc:
        print(f"Archive operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
