from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .economic_calibration import (
    build_conditional_economic_calibration,
)
from .performance_matrix import pair_closed_trades
from .recorder import SessionRecorder
from .stability_validation import (
    build_stability_validation,
)


TRADEABLE_PLAYBOOKS = {
    "trend_structure",
    "weak_level_rejection",
    "level_breakout",
}

SOURCE_RANK = {
    "session_report": 1,
    "analysis_pack": 2,
    "raw_session": 3,
}

TABLE_FILES = {
    "sessions": "sessions.jsonl",
    "trades": "trades.jsonl",
    "hindsight": "hindsight-opportunities.jsonl",
    "interactions": "market-interactions.jsonl",
    "arbiterBlocks": "arbiter-blocks.jsonl",
}


def _json_rows(raw: str) -> list[dict]:
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _load_source(
    path: Path,
    *,
    horizon_seconds: float,
) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = SessionRecorder._read_rows(path)
        report = SessionRecorder._build_session_report(
            path,
            rows,
            horizon_seconds=horizon_seconds,
        )
        return {
            "sourcePath": str(path),
            "sourceKind": "raw_session",
            "report": report,
            "rows": rows,
            "manifest": None,
        }

    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "session-report.json" not in names:
                raise ValueError(
                    f"{path} does not contain session-report.json"
                )
            report = json.loads(
                archive.read("session-report.json").decode("utf-8")
            )
            manifest = (
                json.loads(
                    archive.read("manifest.json").decode("utf-8")
                )
                if "manifest.json" in names
                else None
            )
            rows = (
                _json_rows(
                    archive.read(
                        "session-analysis.jsonl"
                    ).decode("utf-8")
                )
                if "session-analysis.jsonl" in names
                else []
            )
        return {
            "sourcePath": str(path),
            "sourceKind": "analysis_pack",
            "report": report,
            "rows": rows,
            "manifest": manifest,
        }

    if suffix == ".json":
        return {
            "sourcePath": str(path),
            "sourceKind": "session_report",
            "report": _read_json(path),
            "rows": [],
            "manifest": None,
        }

    raise ValueError(
        f"Unsupported research source: {path}"
    )


def _run_window(report: dict) -> tuple[Any, Any]:
    opportunity = report.get("postRunOpportunity") or {}
    hindsight = opportunity.get("hindsight") or {}
    policy = hindsight.get("policy") or {}
    return (
        policy.get("runStartTs"),
        policy.get("runEndTs"),
    )


def _session_source_file(
    report: dict,
    manifest: dict | None,
) -> str:
    if isinstance(manifest, dict):
        source = manifest.get("source") or {}
        if isinstance(source, dict) and source.get("file"):
            return str(source["file"])

    market = report.get("marketData") or {}
    if isinstance(market, dict):
        value = market.get("rawSessionFile")
        if value and value != "session-analysis.jsonl":
            return str(value)

    session = report.get("session") or {}
    if isinstance(session, dict) and session.get("file"):
        return str(session["file"])
    return "unknown-session"


def _session_identity(
    report: dict,
    manifest: dict | None,
) -> str:
    run_summary = report.get("runSummary") or {}
    start_ts, end_ts = _run_window(report)
    session = report.get("session") or {}
    material = {
        "sourceFile": _session_source_file(
            report,
            manifest,
        ),
        "runLabel": (
            run_summary.get("runLabel")
            if isinstance(run_summary, dict)
            else None
        ),
        "runStartTs": start_ts,
        "runEndTs": end_ts,
    }
    # Analysis packs contain compacted event streams, so their event count
    # legitimately differs from the raw source. Use eventCount only as a
    # fallback when the run window is unavailable.
    if start_ts is None and end_ts is None:
        material["eventCountFallback"] = (
            session.get("eventCount")
            if isinstance(session, dict)
            else None
        )
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return digest[:20]


def _session_meta(
    source: dict[str, Any],
) -> dict[str, Any]:
    report = source["report"]
    manifest = source.get("manifest")
    session_id = _session_identity(
        report,
        manifest,
    )
    run_summary = report.get("runSummary") or {}
    start_ts, end_ts = _run_window(report)
    performance = (
        report.get("strategySideRegimePerformance")
        or {}
    )
    perf_summary = performance.get("summary") or {}
    hindsight = (
        (report.get("postRunOpportunity") or {})
        .get("hindsight")
        or {}
    )
    hindsight_summary = hindsight.get("summary") or {}
    market_interaction = (
        report.get("marketInteractionResearch") or {}
    )
    interaction_summary = (
        market_interaction.get("summary") or {}
    )
    session = report.get("session") or {}

    return {
        "sessionId": session_id,
        "sourcePath": source["sourcePath"],
        "sourceKind": source["sourceKind"],
        "sourceFile": _session_source_file(
            report,
            manifest,
        ),
        "runLabel": (
            run_summary.get("runLabel")
            if isinstance(run_summary, dict)
            else None
        ),
        "runStartTs": start_ts,
        "runEndTs": end_ts,
        "eventCount": (
            session.get("eventCount")
            if isinstance(session, dict)
            else None
        ),
        "closedTrades": int(
            perf_summary.get("closedTrades")
            or (
                run_summary.get("closedTrades")
                if isinstance(run_summary, dict)
                else 0
            )
            or 0
        ),
        "realizedPnl": (
            run_summary.get("realizedPnl")
            if isinstance(run_summary, dict)
            else None
        ),
        "hindsightOpportunities": int(
            hindsight_summary.get("opportunities")
            or 0
        ),
        "interactionCheckpoints": int(
            interaction_summary.get("checkpoints")
            or 0
        ),
        "generatedAt": report.get("generatedAt"),
        "eventRowsAvailable": bool(
            source.get("rows")
        ),
    }


def _entry_regime_from_opportunity(
    item: dict,
) -> str:
    snapshots = item.get("marketSnapshots") or {}
    entry = snapshots.get("oracleEntry") or {}
    market_context = entry.get("marketContext") or {}
    local = (
        market_context.get("localRegime")
        if isinstance(market_context, dict)
        else None
    )
    return (
        str(local.get("regime") or "unknown")
        if isinstance(local, dict)
        else "unknown"
    )


def _normalise_trade(
    row: dict,
    meta: dict,
    index: int,
) -> dict:
    value = dict(row)
    value.update({
        "datasetTradeId": (
            f"{meta['sessionId']}:trade:{index}"
        ),
        "sessionId": meta["sessionId"],
        "runLabel": meta.get("runLabel"),
        "sourceFile": meta.get("sourceFile"),
    })
    return value


def _normalise_opportunity(
    row: dict,
    meta: dict,
    index: int,
) -> dict:
    value = dict(row)
    value.update({
        "datasetOpportunityId": (
            f"{meta['sessionId']}:hindsight:{index}"
        ),
        "sessionId": meta["sessionId"],
        "runLabel": meta.get("runLabel"),
        "sourceFile": meta.get("sourceFile"),
        "entryLocalRegime": (
            _entry_regime_from_opportunity(row)
        ),
    })
    return value


def _normalise_interaction(
    row: dict,
    meta: dict,
    index: int,
) -> dict:
    value = dict(row)
    value.update({
        "datasetCheckpointId": (
            f"{meta['sessionId']}:interaction:{index}"
        ),
        "sessionId": meta["sessionId"],
        "runLabel": meta.get("runLabel"),
        "sourceFile": meta.get("sourceFile"),
    })
    return value


def _arbiter_blocks(
    rows: list[dict],
    meta: dict,
) -> list[dict]:
    result = []
    for index, row in enumerate(
        (
            value
            for value in rows
            if value.get("event") == "arbiter_blocked"
        ),
        start=1,
    ):
        payload = row.get("payload") or {}
        result.append({
            "datasetArbiterBlockId": (
                f"{meta['sessionId']}:arbiter:{index}"
            ),
            "sessionId": meta["sessionId"],
            "runLabel": meta.get("runLabel"),
            "sourceFile": meta.get("sourceFile"),
            "ts": row.get("ts"),
            "symbol": row.get("symbol"),
            "strategy": payload.get("strategy"),
            "setupId": payload.get("setupId"),
            "blockers": list(
                payload.get("blockers") or []
            ),
            "conflictingStrategies": list(
                payload.get("conflictingStrategies")
                or []
            ),
            "confluenceStrategies": list(
                payload.get("confluenceStrategies")
                or []
            ),
            "semanticArbitration": (
                payload.get("semanticArbitration")
            ),
        })
    return result


def _session_records(
    source: dict[str, Any],
) -> dict[str, Any]:
    report = source["report"]
    rows = source.get("rows") or []
    meta = _session_meta(source)

    performance = (
        report.get("strategySideRegimePerformance")
        or {}
    )
    trades = list(performance.get("trades") or [])
    if not trades and rows:
        trades = pair_closed_trades(rows)

    opportunity_report = (
        report.get("postRunOpportunity") or {}
    )
    hindsight = opportunity_report.get("hindsight") or {}
    opportunities = list(
        hindsight.get("opportunities") or []
    )

    interactions = (
        report.get("marketInteractionResearch") or {}
    )
    checkpoints = list(
        interactions.get("checkpoints") or []
    )

    return {
        "meta": meta,
        "report": report,
        "trades": [
            _normalise_trade(row, meta, index)
            for index, row in enumerate(
                trades,
                start=1,
            )
        ],
        "hindsight": [
            _normalise_opportunity(row, meta, index)
            for index, row in enumerate(
                opportunities,
                start=1,
            )
        ],
        "interactions": [
            _normalise_interaction(row, meta, index)
            for index, row in enumerate(
                checkpoints,
                start=1,
            )
        ],
        "arbiterBlocks": _arbiter_blocks(
            rows,
            meta,
        ),
    }


def _safe_float(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float))
        else None
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _trade_outcome_summary(
    rows: list[dict],
) -> dict[str, Any]:
    nets = [
        float(row.get("netPnl") or 0.0)
        for row in rows
    ]
    all_in_r = [
        float(value)
        for row in rows
        if (
            value := row.get("realizedAllInR")
        ) is not None
    ]
    mfe = [
        float(value)
        for row in rows
        if (value := row.get("mfeR")) is not None
    ]
    mae = [
        float(value)
        for row in rows
        if (value := row.get("maeR")) is not None
    ]
    session_ids = {
        str(row.get("sessionId") or "")
        for row in rows
    }
    return {
        "samples": len(rows),
        "sessions": len(session_ids),
        "wins": sum(value > 0 for value in nets),
        "losses": sum(value < 0 for value in nets),
        "winRate": (
            sum(value > 0 for value in nets)
            / len(rows)
            if rows
            else None
        ),
        "grossPnl": sum(
            float(row.get("grossPnl") or 0.0)
            for row in rows
        ),
        "fees": sum(
            float(row.get("fees") or 0.0)
            for row in rows
        ),
        "netPnl": sum(nets),
        "netPerTrade": _mean(nets),
        "expectancyAllInR": _mean(all_in_r),
        "medianAllInR": _median(all_in_r),
        "averageMfeR": _mean(mfe),
        "averageMaeR": _mean(mae),
    }


def _feature_outcomes(
    trades: list[dict],
) -> list[dict]:
    dimensions = {
        "flowAlignment": "flowAlignmentClass",
        "entryFreshness": "freshnessClass",
        "liquidityAlignment": (
            "liquidityAlignmentClass"
        ),
        "confluenceCount": "confluenceCount",
    }
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in trades:
        strategy = str(
            row.get("strategy") or "unknown"
        )
        side = str(row.get("side") or "unknown")
        regime = str(
            row.get("regime") or "unknown"
        )
        for dimension, key in dimensions.items():
            raw = row.get(key)
            value = (
                str(int(raw))
                if (
                    dimension == "confluenceCount"
                    and isinstance(raw, (int, float))
                )
                else str(raw or "unknown")
            )
            grouped[
                (
                    strategy,
                    side,
                    regime,
                    dimension,
                    value,
                )
            ].append(row)

    result = []
    for (
        strategy,
        side,
        regime,
        dimension,
        value,
    ), values in sorted(grouped.items()):
        result.append({
            "strategy": strategy,
            "side": side,
            "regime": regime,
            "dimension": dimension,
            "value": value,
            **_trade_outcome_summary(values),
        })
    return result


def _hindsight_coverage(
    opportunities: list[dict],
) -> list[dict]:
    grouped: dict[
        tuple[str, str, str],
        list[dict],
    ] = defaultdict(list)

    for item in opportunities:
        side = str(item.get("side") or "unknown")
        regime = str(
            item.get("entryLocalRegime")
            or "unknown"
        )
        comparison = item.get("botComparison") or {}
        actual_trade = comparison.get("trade")
        actual_strategy = (
            str(actual_trade.get("strategy") or "")
            if isinstance(actual_trade, dict)
            else ""
        )
        bot_class = str(
            comparison.get("classification")
            or "unknown"
        )
        fit = item.get("strategyFit") or {}
        for strategy_row in fit.get("strategies") or []:
            strategy = str(
                strategy_row.get("strategy") or ""
            )
            if strategy not in TRADEABLE_PLAYBOOKS:
                continue
            fit_class = str(
                strategy_row.get("fit")
                or "unaware"
            )
            grouped[
                (
                    strategy,
                    side,
                    regime,
                )
            ].append({
                "fit": fit_class,
                "observed": (
                    fit_class
                    in {
                        "observed_aligned",
                        "tradeable_aligned",
                    }
                ),
                "tradeable": (
                    fit_class == "tradeable_aligned"
                ),
                "traded": (
                    actual_strategy == strategy
                    and bot_class
                    not in {
                        "missed",
                        "wrong_direction",
                    }
                ),
                "botClassification": bot_class,
                "sessionId": item.get("sessionId"),
            })

    result = []
    for (
        strategy,
        side,
        regime,
    ), rows in sorted(grouped.items()):
        total = len(rows)
        observed = sum(
            bool(row["observed"])
            for row in rows
        )
        tradeable = sum(
            bool(row["tradeable"])
            for row in rows
        )
        traded = sum(
            bool(row["traded"])
            for row in rows
        )
        result.append({
            "strategy": strategy,
            "side": side,
            "regime": regime,
            "opportunities": total,
            "sessions": len({
                str(row.get("sessionId") or "")
                for row in rows
            }),
            "observed": observed,
            "tradeable": tradeable,
            "traded": traded,
            "observedCoverageRate": (
                observed / total if total else None
            ),
            "tradeableCoverageRate": (
                tradeable / total if total else None
            ),
            "tradeCoverageRate": (
                traded / total if total else None
            ),
            "fitCounts": dict(Counter(
                str(row["fit"]) for row in rows
            )),
            "botClassificationCounts": dict(
                Counter(
                    str(row["botClassification"])
                    for row in rows
                )
            ),
        })
    return result


def _arbiter_summary(
    blocks: list[dict],
) -> list[dict]:
    grouped: Counter[tuple[str, str]] = Counter()
    sessions: dict[
        tuple[str, str],
        set[str],
    ] = defaultdict(set)
    for row in blocks:
        strategy = str(
            row.get("strategy") or "unknown"
        )
        for blocker in row.get("blockers") or []:
            key = (strategy, str(blocker))
            grouped[key] += 1
            sessions[key].add(
                str(row.get("sessionId") or "")
            )
    return [
        {
            "strategy": strategy,
            "blocker": blocker,
            "count": count,
            "sessions": len(
                sessions[(strategy, blocker)]
            ),
        }
        for (strategy, blocker), count
        in sorted(grouped.items())
    ]


def _interaction_outcomes(
    checkpoints: list[dict],
) -> list[dict]:
    grouped: Counter[tuple] = Counter()
    sessions: dict[tuple, set[str]] = defaultdict(set)
    for row in checkpoints:
        strategy = str(
            row.get("strategy") or "unknown"
        )
        state = str(row.get("state") or "unknown")
        side = str(
            row.get("hypothesisSide") or "unknown"
        )
        forward = row.get("forward") or {}
        for horizon, horizon_data in forward.items():
            if not isinstance(horizon_data, dict):
                continue
            bands = (
                horizon_data.get("movementBands")
                or {}
            )
            for band, outcome in bands.items():
                if not isinstance(outcome, dict):
                    continue
                classification = str(
                    outcome.get("classification")
                    or "unknown"
                )
                key = (
                    strategy,
                    state,
                    side,
                    str(horizon),
                    str(band),
                    classification,
                )
                grouped[key] += 1
                sessions[key].add(
                    str(row.get("sessionId") or "")
                )
    return [
        {
            "strategy": key[0],
            "state": key[1],
            "side": key[2],
            "horizon": key[3],
            "band": key[4],
            "classification": key[5],
            "count": count,
            "sessions": len(sessions[key]),
        }
        for key, count in sorted(grouped.items())
    ]


def aggregate_research_sources(
    sources: Iterable[str | Path],
    *,
    horizon_seconds: float = 120.0,
    minimum_group_samples: int = 20,
    minimum_segment_samples: int = 8,
    minimum_validation_sessions: int = 4,
    minimum_validation_session_samples: int = 2,
    minimum_validation_train_samples: int = 6,
    minimum_interaction_resolved_per_session: int = 2,
    minimum_hindsight_opportunities_per_session: int = 2,
    validation_neutral_epsilon_r: float = 0.05,
    validation_minimum_effect_r: float = 0.10,
    validation_sign_agreement_rate: float = 0.75,
    validation_threshold_consistency_rate: float = 0.50,
) -> dict[str, Any]:
    loaded: dict[str, dict[str, Any]] = {}
    duplicate_sources: list[dict] = []

    for raw_path in sources:
        path = Path(raw_path)
        source = _load_source(
            path,
            horizon_seconds=horizon_seconds,
        )
        records = _session_records(source)
        session_id = records["meta"]["sessionId"]
        if session_id in loaded:
            existing = loaded[session_id]
            existing_rank = SOURCE_RANK.get(
                str(
                    existing["meta"].get(
                        "sourceKind"
                    )
                    or ""
                ),
                0,
            )
            incoming_rank = SOURCE_RANK.get(
                str(
                    records["meta"].get(
                        "sourceKind"
                    )
                    or ""
                ),
                0,
            )
            if incoming_rank > existing_rank:
                duplicate_sources.append({
                    "sessionId": session_id,
                    "kept": records["meta"][
                        "sourcePath"
                    ],
                    "ignored": existing["meta"][
                        "sourcePath"
                    ],
                    "reason": "richer_source_replaced_existing",
                })
                loaded[session_id] = records
            else:
                duplicate_sources.append({
                    "sessionId": session_id,
                    "kept": existing["meta"][
                        "sourcePath"
                    ],
                    "ignored": str(path),
                    "reason": "duplicate_or_lower_fidelity_source",
                })
            continue
        loaded[session_id] = records

    sessions = [
        value["meta"]
        for _, value in sorted(loaded.items())
    ]
    trades = [
        row
        for value in loaded.values()
        for row in value["trades"]
    ]
    hindsight = [
        row
        for value in loaded.values()
        for row in value["hindsight"]
    ]
    interactions = [
        row
        for value in loaded.values()
        for row in value["interactions"]
    ]
    arbiter_blocks = [
        row
        for value in loaded.values()
        for row in value["arbiterBlocks"]
    ]

    calibration = (
        build_conditional_economic_calibration(
            trades,
            minimum_group_samples=(
                minimum_group_samples
            ),
            minimum_segment_samples=(
                minimum_segment_samples
            ),
        )
    )
    stability = build_stability_validation(
        trades=trades,
        hindsight=hindsight,
        interactions=interactions,
        minimum_sessions=minimum_validation_sessions,
        minimum_session_samples=minimum_validation_session_samples,
        minimum_train_samples=minimum_validation_train_samples,
        minimum_interaction_resolved_per_session=(
            minimum_interaction_resolved_per_session
        ),
        minimum_hindsight_opportunities_per_session=(
            minimum_hindsight_opportunities_per_session
        ),
        neutral_epsilon_r=validation_neutral_epsilon_r,
        minimum_effect_r=validation_minimum_effect_r,
        sign_agreement_rate=validation_sign_agreement_rate,
        threshold_consistency_rate=(
            validation_threshold_consistency_rate
        ),
    )

    return {
        "schemaVersion": 1,
        "generatedAt": (
            datetime.now(UTC).isoformat()
        ),
        "policy": {
            "opportunityHorizonSeconds": (
                horizon_seconds
            ),
            "minimumEconomicGroupSamples": (
                minimum_group_samples
            ),
            "minimumEconomicSegmentSamples": (
                minimum_segment_samples
            ),
            "minimumValidationSessions": (
                minimum_validation_sessions
            ),
            "minimumValidationSessionSamples": (
                minimum_validation_session_samples
            ),
            "minimumValidationTrainSamples": (
                minimum_validation_train_samples
            ),
            "minimumInteractionResolvedPerSession": (
                minimum_interaction_resolved_per_session
            ),
            "minimumHindsightOpportunitiesPerSession": (
                minimum_hindsight_opportunities_per_session
            ),
            "validationNeutralEpsilonR": (
                validation_neutral_epsilon_r
            ),
            "validationMinimumEffectR": (
                validation_minimum_effect_r
            ),
            "validationSignAgreementRate": (
                validation_sign_agreement_rate
            ),
            "validationThresholdConsistencyRate": (
                validation_threshold_consistency_rate
            ),
            "sourceIsolation": (
                "Each session is analyzed independently first. "
                "Stage 10 only aggregates already-causal "
                "per-session features and hindsight labels."
            ),
            "hindsightWarning": (
                "Hindsight entry/exit remain future-informed "
                "labels. They are never fed back into live "
                "features inside the source session."
            ),
            "eventCoverageWarning": (
                "Arbiter-block event aggregation is complete only "
                "for sources with raw/session-analysis rows. "
                "Standalone session reports do not contain the "
                "individual arbiter_blocked events."
            ),
        },
        "summary": {
            "sessions": len(sessions),
            "duplicateSourcesIgnored": len(
                duplicate_sources
            ),
            "closedTrades": len(trades),
            "hindsightOpportunities": len(
                hindsight
            ),
            "marketInteractionCheckpoints": len(
                interactions
            ),
            "arbiterBlocks": len(
                arbiter_blocks
            ),
            "sessionsWithEventRows": sum(
                bool(row.get("eventRowsAvailable"))
                for row in sessions
            ),
            "sessionsWithoutEventRows": sum(
                not bool(row.get("eventRowsAvailable"))
                for row in sessions
            ),
            "runLabels": sorted({
                str(row.get("runLabel") or "")
                for row in sessions
                if row.get("runLabel")
            }),
        },
        "sessions": sessions,
        "duplicates": duplicate_sources,
        "tradeFeatureOutcomes": (
            _feature_outcomes(trades)
        ),
        "hindsightCoverage": (
            _hindsight_coverage(hindsight)
        ),
        "marketInteractionOutcomes": (
            _interaction_outcomes(interactions)
        ),
        "arbiterBlockSummary": (
            _arbiter_summary(arbiter_blocks)
        ),
        "conditionalEconomicCalibration": (
            calibration
        ),
        "stabilityValidation": stability,
        "tables": {
            "trades": trades,
            "hindsight": hindsight,
            "interactions": interactions,
            "arbiterBlocks": arbiter_blocks,
        },
    }


def write_research_dataset_pack(
    sources: Iterable[str | Path],
    *,
    output_path: str | Path,
    horizon_seconds: float = 120.0,
    minimum_group_samples: int = 20,
    minimum_segment_samples: int = 8,
    minimum_validation_sessions: int = 4,
    minimum_validation_session_samples: int = 2,
    minimum_validation_train_samples: int = 6,
    minimum_interaction_resolved_per_session: int = 2,
    minimum_hindsight_opportunities_per_session: int = 2,
    validation_neutral_epsilon_r: float = 0.05,
    validation_minimum_effect_r: float = 0.10,
    validation_sign_agreement_rate: float = 0.75,
    validation_threshold_consistency_rate: float = 0.50,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    dataset = aggregate_research_sources(
        sources,
        horizon_seconds=horizon_seconds,
        minimum_group_samples=(
            minimum_group_samples
        ),
        minimum_segment_samples=(
            minimum_segment_samples
        ),
        minimum_validation_sessions=(
            minimum_validation_sessions
        ),
        minimum_validation_session_samples=(
            minimum_validation_session_samples
        ),
        minimum_validation_train_samples=(
            minimum_validation_train_samples
        ),
        minimum_interaction_resolved_per_session=(
            minimum_interaction_resolved_per_session
        ),
        minimum_hindsight_opportunities_per_session=(
            minimum_hindsight_opportunities_per_session
        ),
        validation_neutral_epsilon_r=(
            validation_neutral_epsilon_r
        ),
        validation_minimum_effect_r=(
            validation_minimum_effect_r
        ),
        validation_sign_agreement_rate=(
            validation_sign_agreement_rate
        ),
        validation_threshold_consistency_rate=(
            validation_threshold_consistency_rate
        ),
    )
    tables = dataset.pop("tables")
    manifest = {
        "schemaVersion": 1,
        "generatedAt": dataset["generatedAt"],
        "summary": dataset["summary"],
        "files": {
            "report": "cross-session-report.json",
            "stabilityValidation": "stability-validation.json",
            **TABLE_FILES,
        },
    }

    with tempfile.TemporaryDirectory(
        prefix="scalp-research-dataset-"
    ) as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "cross-session-report.json").write_text(
            json.dumps(
                dataset,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (tmp / "manifest.json").write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (tmp / "stability-validation.json").write_text(
            json.dumps(
                dataset.get("stabilityValidation") or {},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        session_by_id = {
            row["sessionId"]: row
            for row in dataset["sessions"]
        }
        table_values = {
            "sessions": list(
                session_by_id.values()
            ),
            "trades": tables["trades"],
            "hindsight": tables["hindsight"],
            "interactions": tables[
                "interactions"
            ],
            "arbiterBlocks": tables[
                "arbiterBlocks"
            ],
        }
        for key, filename in TABLE_FILES.items():
            with (
                tmp / filename
            ).open(
                "w",
                encoding="utf-8",
            ) as fh:
                for row in table_values[key]:
                    fh.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )

        readme = (
            "Cross-session research dataset for scalp-bot.\n\n"
            "cross-session-report.json: aggregate metrics and hypotheses.\n"
            "stability-validation.json: session holdout / leave-one-session-out validation.\n"
            "sessions.jsonl: session provenance.\n"
            "trades.jsonl: normalized actual closed trades.\n"
            "hindsight-opportunities.jsonl: independent hindsight labels.\n"
            "market-interactions.jsonl: causal interaction checkpoints.\n"
            "arbiter-blocks.jsonl: semantic veto events when source rows were available.\n\n"
            "Do not use oracle entry/exit values as live features. "
            "They are future-informed research labels only.\n"
        )
        (tmp / "README.txt").write_text(
            readme,
            encoding="utf-8",
        )

        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for path in sorted(tmp.iterdir()):
                archive.write(
                    path,
                    path.name,
                )

    return output
