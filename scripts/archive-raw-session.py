from __future__ import annotations

import argparse
from pathlib import Path

from scalp_bot.config import settings
from scalp_bot.raw_session_archive import (
    build_raw_session_archive,
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
            "Create a lossless zstd raw-session archive with "
            "SHA-256 integrity metadata."
        )
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session JSONL path. Defaults to latest session.",
    )
    parser.add_argument(
        "--output-root",
        default="data/raw-archives",
    )
    parser.add_argument("--mirror-root")
    parser.add_argument(
        "--compression-level",
        type=int,
        default=10,
    )
    parser.add_argument("--bot-commit")
    parser.add_argument("--run-profile")
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
    output = build_raw_session_archive(
        source,
        output_root=args.output_root,
        mirror_root=args.mirror_root,
        compression_level=args.compression_level,
        bot_commit=args.bot_commit,
        run_profile=args.run_profile,
        overwrite=args.overwrite,
    )
    print(output)


if __name__ == "__main__":
    main()
