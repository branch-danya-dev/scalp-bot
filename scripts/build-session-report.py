from __future__ import annotations

import argparse
from pathlib import Path

from scalp_bot.config import settings
from scalp_bot.recorder import SessionRecorder


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
        description="Build a post-run analysis report from a recorded scalp-bot session.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Path to session-*.jsonl. Defaults to the latest session in SCALP_SESSION_DIR.",
    )
    parser.add_argument(
        "--horizon",
        type=float,
        default=120.0,
        help="Opportunity-review horizon in seconds (default: 120).",
    )
    args = parser.parse_args()

    session_path = (
        Path(args.session)
        if args.session
        else latest_session(Path(settings.session_dir))
    )
    output = SessionRecorder.write_report_for_file(
        session_path,
        horizon_seconds=args.horizon,
    )
    print(output)


if __name__ == "__main__":
    main()
