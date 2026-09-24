from __future__ import annotations

import argparse
from pathlib import Path

from scalp_bot.config import settings
from scalp_bot.git_session_export import (
    DEFAULT_MAX_FILE_BYTES,
    build_git_session_export,
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
            "Build a Git-friendly sharded analysis tree from a scalp-bot "
            "research session without committing the raw JSONL."
        )
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session JSONL path. Defaults to latest session.",
    )
    parser.add_argument(
        "--output-root",
        default="data/git-exports",
    )
    parser.add_argument(
        "--shard-minutes",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--max-file-mb",
        type=float,
        default=DEFAULT_MAX_FILE_BYTES / (1024 * 1024),
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
    parser.add_argument("--book-depth", type=int, default=16)
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
    parser.add_argument(
        "--overwrite",
        action="store_true",
    )
    args = parser.parse_args()

    source = (
        Path(args.session)
        if args.session
        else latest_session(Path(settings.session_dir))
    )
    max_bytes = int(args.max_file_mb * 1024 * 1024)
    if max_bytes <= 0:
        raise SystemExit("--max-file-mb must be positive")

    output = build_git_session_export(
        source,
        output_root=args.output_root,
        shard_minutes=args.shard_minutes,
        max_file_bytes=max_bytes,
        overview_frame_seconds=args.overview_frame_seconds,
        shard_frame_seconds=args.shard_frame_seconds,
        book_depth=args.book_depth,
        focus_book_depth=args.focus_book_depth,
        book_sample_seconds=args.book_sample_seconds,
        horizon_seconds=args.horizon,
        overwrite=args.overwrite,
    )
    print(output)


if __name__ == "__main__":
    main()
