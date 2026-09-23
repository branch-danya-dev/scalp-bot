from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any


TRADEABLE_PLAYBOOKS = {
    "trend_structure",
    "weak_level_rejection",
    "level_breakout",
}


def _row_ts(row: dict) -> float:
    raw = row.get("ts")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _setup_id_from_open(payload: dict) -> str | None:
    plan = payload.get("plan") or {}
    position = payload.get("position") or {}
    return (
        plan.get("setup_id")
        or plan.get("setupId")
        or position.get("setup_id")
        or position.get("setupId")
        or payload.get("setupId")
    )


def _setup_id_from_close(payload: dict) -> str | None:
    return payload.get("setupId") or payload.get("setup_id")


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _extract_regime(strategy_details: dict) -> str:
    decision_context = (
        strategy_details.get("decisionContext")
        if isinstance(strategy_details, dict)
        else None
    )
    if isinstance(decision_context, dict):
        return str(
            decision_context.get("localRegime")
            or "unknown"
        )
    return "unknown"


def _extract_entry_features(strategy_details: dict) -> dict[str, Any]:
    details = strategy_details if isinstance(strategy_details, dict) else {}
    freshness = details.get("entryFreshness")
    freshness = freshness if isinstance(freshness, dict) else {}
    flow = details.get("flowAlignment")
    flow = flow if isinstance(flow, dict) else {}
    liquidity = details.get("liquidityAlignment")
    liquidity = liquidity if isinstance(liquidity, dict) else {}
    arbitration = details.get("semanticArbitration")
    arbitration = arbitration if isinstance(arbitration, dict) else {}
    decision_context = details.get("decisionContext")
    decision_context = decision_context if isinstance(decision_context, dict) else {}
    return {
        "regime": str(decision_context.get("localRegime") or "unknown"),
        "htfBias": str(decision_context.get("htfBias") or "unknown"),
        "executionReady": decision_context.get("executionReady"),
        "freshnessClass": str(freshness.get("classification") or "unknown"),
        "moveSpentRatio": _safe_float(freshness.get("moveSpentRatio")),
        "confirmationAgeSeconds": _safe_float(
            freshness.get("confirmationAgeSeconds")
        ),
        "flowAlignmentClass": str(
            flow.get("classification") or "unknown"
        ),
        "flowAlignmentScore": _safe_float(flow.get("score")),
        "liquidityAlignmentClass": str(
            liquidity.get("classification") or "unknown"
        ),
        "liquidityAlignmentScore": _safe_float(
            liquidity.get("score")
        ),
        "confluenceCount": int(
            arbitration.get("confluenceCount") or 0
        ),
    }


def pair_closed_trades(rows: list[dict]) -> list[dict]:
    pending: dict[
        tuple[str, str | None],
        list[dict],
    ] = defaultdict(list)
    result: list[dict] = []

    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        event = str(row.get("event") or "")
        payload = row.get("payload") or {}

        if event == "trade_opened":
            plan = payload.get("plan") or {}
            position = payload.get("position") or {}
            setup_id = _setup_id_from_open(payload)
            strategy_details = (
                plan.get("strategy_details")
                if isinstance(plan, dict)
                else {}
            ) or {}
            features = _extract_entry_features(strategy_details)
            pending[
                (
                    symbol,
                    str(setup_id)
                    if setup_id is not None
                    else None,
                )
            ].append({
                "symbol": symbol,
                "openTs": _row_ts(row),
                "strategy": str(
                    plan.get("strategy")
                    or payload.get("strategy")
                    or ""
                ),
                "side": str(
                    position.get("side")
                    or plan.get("side")
                    or ""
                ),
                "setupId": setup_id,
                "plannedNetRewardRisk": _safe_float(
                    (
                        plan.get("net_reward_risk")
                        if plan.get("net_reward_risk") is not None
                        else plan.get("netRewardRisk")
                    )
                ),
                "entry": _safe_float(
                    position.get("entry")
                    or plan.get("market_entry")
                    or plan.get("marketEntry")
                ),
                "strategyDetails": strategy_details,
                **features,
            })
            continue

        if event != "trade_closed":
            continue

        setup_id = _setup_id_from_close(payload)
        key = (
            symbol,
            str(setup_id)
            if setup_id is not None
            else None,
        )
        queue = pending.get(key)
        if not queue:
            fallback = next(
                (
                    candidate
                    for candidate, values in pending.items()
                    if candidate[0] == symbol and values
                ),
                None,
            )
            queue = pending.get(fallback) if fallback else None
        if not queue:
            continue

        trade = queue.pop(0)
        close_ts = _row_ts(row)
        initial_risk = _safe_float(payload.get("initialRiskUsd"))
        net = float(payload.get("netPnl") or 0.0)
        realized_r = (
            net / initial_risk
            if initial_risk is not None and initial_risk > 0
            else None
        )
        trade.update({
            "closeTs": close_ts,
            "durationSeconds": max(
                0.0,
                close_ts - float(trade["openTs"]),
            ),
            "exit": _safe_float(payload.get("exit")),
            "grossPnl": float(payload.get("grossPnl") or 0.0),
            "fees": float(payload.get("fees") or 0.0),
            "netPnl": net,
            "realizedR": realized_r,
            "mfeR": _safe_float(payload.get("mfeR")),
            "maeR": _safe_float(payload.get("maeR")),
            "mfeBps": _safe_float(
                payload.get("maxFavorableMoveBps")
            ),
            "maeBps": _safe_float(
                payload.get("maxAdverseMoveBps")
            ),
            "partialTaken": bool(payload.get("partialTaken")),
            "exitReason": str(
                payload.get("reason") or "unknown"
            ),
        })
        result.append(trade)

    return result


def _mean(values: list[float]) -> float | None:
    return (
        sum(values) / len(values)
        if values
        else None
    )


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _counter(rows: list[dict], key: str) -> dict[str, int]:
    counts = Counter(
        str(row.get(key) or "unknown")
        for row in rows
    )
    return dict(sorted(counts.items()))


def _summarize_trade_group(
    rows: list[dict],
    *,
    strategy: str,
    side: str,
    regime: str,
) -> dict[str, Any]:
    nets = [float(row["netPnl"]) for row in rows]
    gross = [float(row["grossPnl"]) for row in rows]
    fees = [float(row["fees"]) for row in rows]
    realized_r = [
        float(value)
        for row in rows
        if (value := row.get("realizedR")) is not None
    ]
    mfe_r = [
        float(value)
        for row in rows
        if (value := row.get("mfeR")) is not None
    ]
    mae_r = [
        float(value)
        for row in rows
        if (value := row.get("maeR")) is not None
    ]
    planned_rr = [
        float(value)
        for row in rows
        if (
            value := row.get("plannedNetRewardRisk")
        ) is not None
    ]
    spent = [
        float(value)
        for row in rows
        if (value := row.get("moveSpentRatio")) is not None
    ]
    ages = [
        float(value)
        for row in rows
        if (
            value := row.get("confirmationAgeSeconds")
        ) is not None
    ]
    durations = [
        float(row.get("durationSeconds") or 0.0)
        for row in rows
    ]
    wins = sum(value > 0 for value in nets)
    losses = sum(value < 0 for value in nets)
    breakeven = len(rows) - wins - losses
    return {
        "strategy": strategy,
        "side": side,
        "regime": regime,
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "winRate": (
            wins / len(rows)
            if rows
            else None
        ),
        "grossPnl": sum(gross),
        "fees": sum(fees),
        "netPnl": sum(nets),
        "averageNetPnl": _mean(nets),
        "averageRealizedR": _mean(realized_r),
        "medianRealizedR": _median(realized_r),
        "averageMfeR": _mean(mfe_r),
        "medianMfeR": _median(mfe_r),
        "averageMaeR": _mean(mae_r),
        "medianMaeR": _median(mae_r),
        "averagePlannedNetRewardRisk": _mean(planned_rr),
        "medianPlannedNetRewardRisk": _median(planned_rr),
        "averageEntryMoveSpentRatio": _mean(spent),
        "medianEntryMoveSpentRatio": _median(spent),
        "averageConfirmationAgeSeconds": _mean(ages),
        "medianConfirmationAgeSeconds": _median(ages),
        "averageDurationSeconds": _mean(durations),
        "partialTakeRate": (
            sum(bool(row.get("partialTaken")) for row in rows)
            / len(rows)
            if rows
            else None
        ),
        "freshnessCounts": _counter(
            rows,
            "freshnessClass",
        ),
        "flowAlignmentCounts": _counter(
            rows,
            "flowAlignmentClass",
        ),
        "liquidityAlignmentCounts": _counter(
            rows,
            "liquidityAlignmentClass",
        ),
        "htfBiasCounts": _counter(rows, "htfBias"),
        "exitReasonCounts": _counter(rows, "exitReason"),
        "confluenceCounts": dict(sorted(Counter(
            str(int(row.get("confluenceCount") or 0))
            for row in rows
        ).items())),
    }


def _group_trade_rows(
    trades: list[dict],
    key_fn,
) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in trades:
        grouped[key_fn(row)].append(row)

    result = []
    for key, values in sorted(grouped.items()):
        strategy, side, regime = key
        result.append(
            _summarize_trade_group(
                values,
                strategy=strategy,
                side=side,
                regime=regime,
            )
        )
    return result


def _oracle_regime(item: dict) -> str:
    snapshots = item.get("marketSnapshots") or {}
    entry = snapshots.get("oracleEntry") or {}
    market_context = entry.get("marketContext") or {}
    local = (
        market_context.get("localRegime")
        if isinstance(market_context, dict)
        else None
    )
    if isinstance(local, dict):
        return str(local.get("regime") or "unknown")
    return "unknown"


def _hindsight_rows(
    hindsight: dict | None,
) -> list[dict]:
    if not isinstance(hindsight, dict):
        return []

    result: list[dict] = []
    for item in hindsight.get("opportunities") or []:
        side = str(item.get("side") or "unknown")
        regime = _oracle_regime(item)
        bot = item.get("botComparison") or {}
        actual_trade = bot.get("trade")
        actual_strategy = (
            str(actual_trade.get("strategy") or "")
            if isinstance(actual_trade, dict)
            else ""
        )
        classification = str(
            bot.get("classification") or "unknown"
        )
        strategy_fit = item.get("strategyFit") or {}
        for fit in strategy_fit.get("strategies") or []:
            strategy = str(fit.get("strategy") or "")
            if (
                not strategy
                or strategy not in TRADEABLE_PLAYBOOKS
            ):
                continue
            fit_class = str(fit.get("fit") or "unaware")
            result.append({
                "strategy": strategy,
                "side": side,
                "regime": regime,
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
                "tradedByThisStrategy": (
                    bool(actual_strategy)
                    and actual_strategy == strategy
                    and classification
                    not in {"missed", "wrong_direction"}
                ),
                "botClassification": classification,
            })
    return result


def _summarize_hindsight(
    rows: list[dict],
) -> list[dict]:
    grouped: dict[
        tuple[str, str, str],
        list[dict],
    ] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row["strategy"],
                row["side"],
                row["regime"],
            )
        ].append(row)

    result = []
    for (strategy, side, regime), values in sorted(
        grouped.items()
    ):
        opportunities = len(values)
        observed = sum(bool(row["observed"]) for row in values)
        tradeable = sum(bool(row["tradeable"]) for row in values)
        traded = sum(
            bool(row["tradedByThisStrategy"])
            for row in values
        )
        result.append({
            "strategy": strategy,
            "side": side,
            "regime": regime,
            "opportunities": opportunities,
            "observed": observed,
            "tradeable": tradeable,
            "traded": traded,
            "observedCoverageRate": (
                observed / opportunities
                if opportunities
                else None
            ),
            "tradeableCoverageRate": (
                tradeable / opportunities
                if opportunities
                else None
            ),
            "tradeCoverageRate": (
                traded / opportunities
                if opportunities
                else None
            ),
            "fitCounts": _counter(values, "fit"),
            "botClassificationCounts": _counter(
                values,
                "botClassification",
            ),
        })
    return result


def _side_summary(
    trades: list[dict],
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in trades:
        grouped[str(row.get("side") or "unknown")].append(row)
    result = []
    for side, values in sorted(grouped.items()):
        summary = _summarize_trade_group(
            values,
            strategy="all",
            side=side,
            regime="all",
        )
        result.append(summary)
    return result


def build_strategy_side_regime_report(
    rows: list[dict],
    *,
    hindsight: dict | None = None,
) -> dict[str, Any]:
    trades = pair_closed_trades(rows)
    by_strategy_side_regime = _group_trade_rows(
        trades,
        lambda row: (
            str(row.get("strategy") or "unknown"),
            str(row.get("side") or "unknown"),
            str(row.get("regime") or "unknown"),
        ),
    )
    by_strategy_side = _group_trade_rows(
        trades,
        lambda row: (
            str(row.get("strategy") or "unknown"),
            str(row.get("side") or "unknown"),
            "all",
        ),
    )
    by_side_regime = _group_trade_rows(
        trades,
        lambda row: (
            "all",
            str(row.get("side") or "unknown"),
            str(row.get("regime") or "unknown"),
        ),
    )
    hindsight_rows = _hindsight_rows(hindsight)
    hindsight_matrix = _summarize_hindsight(
        hindsight_rows
    )

    return {
        "schemaVersion": 1,
        "summary": {
            "closedTrades": len(trades),
            "longTrades": sum(
                row.get("side") == "long"
                for row in trades
            ),
            "shortTrades": sum(
                row.get("side") == "short"
                for row in trades
            ),
            "regimes": sorted({
                str(row.get("regime") or "unknown")
                for row in trades
            }),
            "strategies": sorted({
                str(row.get("strategy") or "unknown")
                for row in trades
            }),
        },
        "sideSummary": _side_summary(trades),
        "byStrategySide": by_strategy_side,
        "bySideRegime": by_side_regime,
        "byStrategySideRegime": by_strategy_side_regime,
        "hindsightByStrategySideRegime": hindsight_matrix,
        "trades": trades,
    }
