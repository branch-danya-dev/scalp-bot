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
        "--min-validation-sessions",
        type=int,
        default=4,
        help="Minimum comparable sessions for stability validation.",
    )
    parser.add_argument(
        "--min-validation-session-samples",
        type=int,
        default=2,
        help="Minimum selected/comparator samples inside one holdout session.",
    )
    parser.add_argument(
        "--min-validation-train-samples",
        type=int,
        default=6,
        help="Minimum selected/comparator samples in leave-one-session-out training folds.",
    )
    parser.add_argument(
        "--min-interaction-resolved-per-session",
        type=int,
        default=2,
        help="Minimum hypothesis/opposite resolved interaction checkpoints per session.",
    )
    parser.add_argument(
        "--min-hindsight-opportunities-per-session",
        type=int,
        default=2,
        help="Minimum hindsight opportunities per session for coverage stability.",
    )
    parser.add_argument(
        "--validation-neutral-epsilon-r",
        type=float,
        default=0.05,
        help="Absolute all-in-R effect treated as neutral.",
    )
    parser.add_argument(
        "--validation-min-effect-r",
        type=float,
        default=0.10,
        help="Minimum pooled/median all-in-R effect for a stable candidate.",
    )
    parser.add_argument(
        "--validation-sign-agreement-rate",
        type=float,
        default=0.75,
        help="Required leave-one-session-out sign agreement rate.",
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
    if args.min_validation_sessions <= 0:
        raise SystemExit(
            "--min-validation-sessions must be positive"
        )
    if args.min_validation_session_samples <= 0:
        raise SystemExit(
            "--min-validation-session-samples must be positive"
        )
    if args.min_validation_train_samples <= 0:
        raise SystemExit(
            "--min-validation-train-samples must be positive"
        )
    if args.min_interaction_resolved_per_session <= 0:
        raise SystemExit(
            "--min-interaction-resolved-per-session must be positive"
        )
    if args.min_hindsight_opportunities_per_session <= 0:
        raise SystemExit(
            "--min-hindsight-opportunities-per-session must be positive"
        )
    if args.validation_neutral_epsilon_r < 0:
        raise SystemExit(
            "--validation-neutral-epsilon-r cannot be negative"
        )
    if args.validation_min_effect_r < 0:
        raise SystemExit(
            "--validation-min-effect-r cannot be negative"
        )
    if not 0 < args.validation_sign_agreement_rate <= 1:
        raise SystemExit(
            "--validation-sign-agreement-rate must be in (0, 1]"
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
        minimum_validation_sessions=(
            args.min_validation_sessions
        ),
        minimum_validation_session_samples=(
            args.min_validation_session_samples
        ),
        minimum_validation_train_samples=(
            args.min_validation_train_samples
        ),
        minimum_interaction_resolved_per_session=(
            args.min_interaction_resolved_per_session
        ),
        minimum_hindsight_opportunities_per_session=(
            args.min_hindsight_opportunities_per_session
        ),
        validation_neutral_epsilon_r=(
            args.validation_neutral_epsilon_r
        ),
        validation_minimum_effect_r=(
            args.validation_min_effect_r
        ),
        validation_sign_agreement_rate=(
            args.validation_sign_agreement_rate
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
