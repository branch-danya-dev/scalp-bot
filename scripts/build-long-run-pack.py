from __future__ import annotations

import argparse
from pathlib import Path

from scalp_bot.config import settings
from scalp_bot.long_run_pack import (
    build_long_run_analysis_bundle,
)


def latest_session(directory: Path) -> Path:
    sessions = sorted(
        directory.glob("session-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not sessions:
        raise SystemExit(
            f"No session-*.jsonl files found in {directory}"
        )
    return sessions[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a small overview archive plus hourly shard archives "
            "from a long scalp-bot research session."
        ),
    )
    parser.add_argument(
        "session",
        nargs="?",
        help=(
            "Path to session-*.jsonl. Defaults to the latest "
            "session in SCALP_SESSION_DIR."
        ),
    )
    parser.add_argument(
        "--output-dir",
        help="Output bundle directory.",
    )
    parser.add_argument(
        "--shard-minutes",
        type=float,
        default=60.0,
    )
    parser.add_argument(
        "--overview-frame-seconds",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--shard-frame-seconds",
        type=float,
        default=3.0,
    )
    parser.add_argument(
        "--book-depth",
        type=int,
        default=16,
    )
    parser.add_argument(
        "--focus-book-depth",
        type=int,
        default=50,
    )
    parser.add_argument(
        "--book-sample-seconds",
        type=float,
        default=15.0,
    )
    parser.add_argument(
        "--horizon",
        type=float,
        default=120.0,
    )
    args = parser.parse_args()

    source = (
        Path(args.session)
        if args.session
        else latest_session(
            Path(settings.session_dir)
        )
    )
    bundle = build_long_run_analysis_bundle(
        source,
        output_dir=args.output_dir,
        shard_seconds=(
            args.shard_minutes * 60.0
        ),
        overview_frame_seconds=(
            args.overview_frame_seconds
        ),
        shard_frame_seconds=(
            args.shard_frame_seconds
        ),
        shard_book_depth=args.book_depth,
        focus_book_depth=args.focus_book_depth,
        orderbook_sample_seconds=(
            args.book_sample_seconds
        ),
        opportunity_horizon_seconds=args.horizon,
    )

    overview = next(
        bundle.glob("*-overview.zip"),
        None,
    )
    shards = sorted(
        bundle.glob("*-hour-*.zip")
    )
    print(f"Bundle: {bundle}")
    if overview is not None:
        print(
            "Overview: "
            f"{overview} "
            f"({overview.stat().st_size / (1024 * 1024):.1f} MB)"
        )
    print(
        f"Hourly shards: {len(shards)}"
    )
    for shard in shards:
        print(
            f"  {shard.name}: "
            f"{shard.stat().st_size / (1024 * 1024):.1f} MB"
        )


if __name__ == "__main__":
    main()
