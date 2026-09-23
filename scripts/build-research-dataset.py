from __future__ import annotations

import argparse
import glob
from datetime import UTC, datetime
from pathlib import Path

from scalp_bot.config import settings
from scalp_bot.research_dataset import (
    write_research_dataset_pack,
)


def resolve_sources(
    patterns: list[str],
    *,
    directory: Path,
    last: int | None,
) -> list[Path]:
    if patterns:
        resolved: list[Path] = []
        for pattern in patterns:
            matches = [
                Path(value)
                for value in glob.glob(
                    pattern,
                    recursive=True,
                )
            ]
            if matches:
                resolved.extend(matches)
            else:
                candidate = Path(pattern)
                if candidate.exists():
                    resolved.append(candidate)
        unique = {
            path.resolve(): path
            for path in resolved
            if path.is_file()
        }
        rows = list(unique.values())
    else:
        # Default to raw sessions only. Derived reports/packs for the same
        # session would otherwise be duplicate inputs by construction.
        rows = list(
            directory.glob("session-*.jsonl")
        )

    rows.sort(
        key=lambda path: path.stat().st_mtime
    )
    if last is not None:
        rows = rows[-max(1, last):]
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a cross-session scalp-bot research dataset from "
            "raw session JSONL files, session reports, and/or analysis packs."
        ),
    )
    parser.add_argument(
        "sources",
        nargs="*",
        help=(
            "Files or glob patterns. Supported: session-*.jsonl, "
            "*-report.json, *-analysis-pack.zip. "
            "Defaults to all raw sessions in SCALP_SESSION_DIR."
        ),
    )
    parser.add_argument(
        "--last",
        type=int,
        help="Use only the N most recently modified resolved sources.",
    )
    parser.add_argument(
        "--horizon",
        type=float,
        default=120.0,
        help="Opportunity-review horizon when raw sessions need analysis.",
    )
    parser.add_argument(
        "--min-group-samples",
        type=int,
        default=20,
        help="Cross-session economic calibration exact-group readiness.",
    )
    parser.add_argument(
        "--min-segment-samples",
        type=int,
        default=8,
        help="Cross-session economic threshold-segment readiness.",
    )
    parser.add_argument(
        "--output",
        help="Output ZIP path.",
    )
    args = parser.parse_args()

    directory = Path(settings.session_dir)
    sources = resolve_sources(
        args.sources,
        directory=directory,
        last=args.last,
    )
    if not sources:
        raise SystemExit(
            "No research sources found. Provide session files/patterns "
            f"or place session-*.jsonl under {directory}."
        )

    if args.min_group_samples <= 0:
        raise SystemExit(
            "--min-group-samples must be positive"
        )
    if args.min_segment_samples <= 0:
        raise SystemExit(
            "--min-segment-samples must be positive"
        )

    output = (
        Path(args.output)
        if args.output
        else directory
        / (
            "research-dataset-"
            + datetime.now(UTC).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            + ".zip"
        )
    )
    result = write_research_dataset_pack(
        sources,
        output_path=output,
        horizon_seconds=args.horizon,
        minimum_group_samples=(
            args.min_group_samples
        ),
        minimum_segment_samples=(
            args.min_segment_samples
        ),
    )
    size_mb = (
        result.stat().st_size
        / (1024 * 1024)
    )
    print(
        f"{result} ({size_mb:.1f} MB) "
        f"from {len(sources)} resolved source(s)"
    )


if __name__ == "__main__":
    main()
