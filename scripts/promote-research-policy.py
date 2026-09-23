from __future__ import annotations

import argparse
import json
from pathlib import Path

from scalp_bot.research_policy import (
    create_policy_manifest,
    load_policy_candidates,
    load_policy_manifest,
    write_policy_manifest,
)


def print_candidates(catalog: dict) -> None:
    rows = catalog.get("candidates") or []
    if not rows:
        print("No Stage 11 promotion candidates found.")
        return
    for row in rows:
        if row.get("ruleType") == "block_feature_value":
            description = (
                f"{row.get('strategy')} {str(row.get('side')).upper()} "
                f"{row.get('regime')} :: block "
                f"{row.get('dimension')}={row.get('value')}"
            )
        else:
            description = (
                f"{row.get('strategy')} {str(row.get('side')).upper()} "
                f"{row.get('regime')} :: min net R:R "
                f"{float(row.get('threshold') or 0):.2f}"
            )
        print(
            f"{row.get('candidateId')}  "
            f"[{row.get('validationStatus')}]  "
            f"{description}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "List validated Stage 11 policy candidates or explicitly "
            "promote selected candidates into a versioned research policy."
        ),
    )
    parser.add_argument(
        "source",
        help=(
            "research-dataset ZIP, cross-session-report.json, "
            "or stability-validation.json"
        ),
    )
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        help=(
            "Candidate id to promote. Repeat for multiple rules. "
            "Without --candidate the script only lists candidates."
        ),
    )
    parser.add_argument(
        "--output",
        help="Policy JSON output path. Required when promoting candidates.",
    )
    parser.add_argument(
        "--version",
        type=int,
        default=1,
        help="Positive policy version number.",
    )
    parser.add_argument(
        "--reason",
        default="",
        help="Required human-readable promotion reason.",
    )
    parser.add_argument(
        "--allow-enforce",
        action="store_true",
        help=(
            "Allow this policy manifest to run in enforce mode. "
            "Without this flag the manifest is shadow-only."
        ),
    )
    parser.add_argument(
        "--previous-policy",
        help=(
            "Previous policy JSON for rollback provenance. "
            "This does not activate or modify that policy."
        ),
    )
    parser.add_argument(
        "--catalog-output",
        help="Optional path to write the candidate catalog JSON.",
    )
    args = parser.parse_args()

    catalog = load_policy_candidates(args.source)
    print_candidates(catalog)

    if args.catalog_output:
        catalog_path = Path(args.catalog_output)
        catalog_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        catalog_path.write_text(
            json.dumps(
                catalog,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"candidate catalog: {catalog_path}")

    if not args.candidate:
        return

    if not args.output:
        raise SystemExit(
            "--output is required when --candidate is used"
        )
    if args.version <= 0:
        raise SystemExit("--version must be positive")
    if not args.reason.strip():
        raise SystemExit(
            "--reason is required for policy promotion"
        )

    previous = (
        load_policy_manifest(
            args.previous_policy
        )
        if args.previous_policy
        else None
    )
    manifest = create_policy_manifest(
        catalog,
        args.candidate,
        version=args.version,
        reason=args.reason.strip(),
        allow_enforce=args.allow_enforce,
        previous_policy=previous,
    )
    result = write_policy_manifest(
        args.output,
        manifest,
    )
    print(
        f"policy: {result} "
        f"id={manifest['policyId']} "
        f"version={manifest['version']} "
        f"allowEnforce={manifest['allowEnforce']}"
    )
    if manifest["rollback"]["previousPolicyId"]:
        print(
            "rollback target: "
            f"{manifest['rollback']['previousPolicyId']} "
            f"v{manifest['rollback']['previousPolicyVersion']}"
        )


if __name__ == "__main__":
    main()
