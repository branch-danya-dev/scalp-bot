from __future__ import annotations

import argparse
import json
from pathlib import Path

from scalp_bot.config import settings
from scalp_bot.market_interaction_review import (
    DEFAULT_HORIZONS_SECONDS,
    DEFAULT_MAX_SHARED_LEVEL_DISTANCE_PCT,
    DEFAULT_MOVE_BANDS,
    analyze_market_interactions,
)
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


def parse_floats(raw: str, *, label: str) -> tuple[float, ...]:
    try:
        values = tuple(
            float(item.strip())
            for item in raw.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise SystemExit(f"Invalid {label}: {raw}") from exc
    if not values or any(value <= 0 for value in values):
        raise SystemExit(f"{label} must contain positive comma-separated numbers")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a market-interaction research report from a recorded "
            "scalp-bot session."
        ),
    )
    parser.add_argument(
        "session",
        nargs="?",
        help=(
            "Path to session-*.jsonl. Defaults to the latest session in "
            "SCALP_SESSION_DIR."
        ),
    )
    parser.add_argument(
        "--horizons",
        default=",".join(f"{value:g}" for value in DEFAULT_HORIZONS_SECONDS),
        help="Forward horizons in seconds, comma-separated.",
    )
    parser.add_argument(
        "--bands",
        default=",".join(f"{value:g}" for value in DEFAULT_MOVE_BANDS),
        help=(
            "Directional move thresholds as decimal fractions, comma-separated "
            "(for example 0.001 = 0.10%%)."
        ),
    )
    parser.add_argument(
        "--max-level-distance-pct",
        type=float,
        default=DEFAULT_MAX_SHARED_LEVEL_DISTANCE_PCT,
        help=(
            "Maximum relative distance between two strategy reference levels "
            "for conflict/confluence analysis."
        ),
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path.",
    )
    args = parser.parse_args()

    session_path = (
        Path(args.session)
        if args.session
        else latest_session(Path(settings.session_dir))
    )
    if not session_path.is_file():
        raise SystemExit(f"Session file not found: {session_path}")

    horizons = parse_floats(args.horizons, label="horizons")
    bands = parse_floats(args.bands, label="bands")
    if args.max_level_distance_pct <= 0:
        raise SystemExit("--max-level-distance-pct must be positive")

    rows = SessionRecorder._read_rows(session_path)
    report = analyze_market_interactions(
        rows,
        horizons_seconds=horizons,
        move_bands=bands,
        max_shared_level_distance_pct=args.max_level_distance_pct,
    )

    output = (
        Path(args.output)
        if args.output
        else session_path.with_name(
            f"{session_path.stem}-market-interactions.json"
        )
    )
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
