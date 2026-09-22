from __future__ import annotations

import argparse
from pathlib import Path

from scalp_bot.analysis_pack import build_analysis_pack
from scalp_bot.config import settings


def latest_session(directory: Path) -> Path:
    sessions = sorted(
        directory.glob("session-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not sessions:
        raise SystemExit(f"No session-*.jsonl files found in {directory}")
    return sessions[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a compact ZIP for ChatGPT/Claude analysis from a large scalp-bot session.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Path to session-*.jsonl. Defaults to the latest session.",
    )
    parser.add_argument("--output", help="Output ZIP path.")
    parser.add_argument("--horizon", type=float, default=120.0)
    parser.add_argument("--book-sample-seconds", type=float, default=5.0)
    parser.add_argument("--book-depth", type=int, default=16)
    parser.add_argument("--focus-book-depth", type=int, default=50)
    args = parser.parse_args()

    source = (
        Path(args.session)
        if args.session
        else latest_session(Path(settings.session_dir))
    )
    output = build_analysis_pack(
        source,
        output_path=args.output,
        opportunity_horizon_seconds=args.horizon,
        orderbook_sample_seconds=args.book_sample_seconds,
        orderbook_sample_depth=args.book_depth,
        focus_orderbook_depth=args.focus_book_depth,
    )
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"{output} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
