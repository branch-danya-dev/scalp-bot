"""Verify a sealed ordinary paper capture against its recorded source."""
import argparse
import asyncio
from pathlib import Path
import sys

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    if args.source_root is not None:
        source = args.source_root.resolve()
        if source != Path(__file__).resolve().parents[1] and not any(Path(p).resolve() == source for p in sys.path):
            sys.path.insert(0, str(source))
    from scalp_bot.capture_replay import verify_capture
    try:
        result = asyncio.run(verify_capture(args.directory))
        print(f"Replay matched: {result['inputs']} inputs, {result['closedTrades']} trades; {args.directory / 'capture-check.json'}")
    except Exception as exc:
        parser.exit(1, f"Capture check failed: {type(exc).__name__}: {exc}\n")
