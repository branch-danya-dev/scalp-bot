import json
import zipfile

import pytest

from scalp_bot.research_dataset import (
    aggregate_research_sources,
    write_research_dataset_pack,
)


def trade(
    *,
    strategy="trend_structure",
    side="long",
    regime="bullish_trend",
    net=2.0,
    realized_all_in_r=0.4,
    flow="aligned",
    freshness="fresh",
    liquidity="supportive",
    confluence=0,
    rr=1.2,
):
    return {
        "strategy": strategy,
        "side": side,
        "regime": regime,
        "netPnl": net,
        "grossPnl": net + 1.0,
        "fees": 1.0,
        "realizedAllInR": realized_all_in_r,
        "realizedR": realized_all_in_r,
        "mfeR": max(0.0, realized_all_in_r + 0.5),
        "maeR": 0.2,
        "flowAlignmentClass": flow,
        "freshnessClass": freshness,
        "liquidityAlignmentClass": liquidity,
        "confluenceCount": confluence,
        "plannedNetRewardRisk": rr,
        "plannedNetAtTargetUsd": rr * 5.0,
        "plannedAllInLossUsd": 5.0,
        "winnerCostShare": 0.25,
        "stopCostShare": 0.3,
        "firstTakeMovePct": 0.003,
        "wouldFailMinimumNetProfit": False,
        "wouldFailNetRewardRisk": rr < 1.15,
        "wouldFailFirstTakeMove": False,
    }


def opportunity(
    *,
    opportunity_id="hindsight-1",
    side="long",
    regime="bullish_trend",
    fit="observed_aligned",
    bot_class="missed",
    actual_strategy=None,
):
    return {
        "opportunityId": opportunity_id,
        "symbol": "AAAUSDT",
        "side": side,
        "grossMovePct": 0.006,
        "estimatedNetMovePct": 0.004,
        "oracleEntryTs": 10.0,
        "oracleExitTs": 30.0,
        "marketSnapshots": {
            "oracleEntry": {
                "marketContext": {
                    "localRegime": {
                        "regime": regime,
                    }
                }
            }
        },
        "strategyFit": {
            "closestPlaybook": (
                "trend_structure"
                if fit != "unaware"
                else None
            ),
            "mappedToExistingStrategy": (
                fit != "unaware"
            ),
            "strategies": [
                {
                    "strategy": "trend_structure",
                    "fit": fit,
                },
                {
                    "strategy": "orderbook_density",
                    "fit": "observed_aligned",
                },
            ],
        },
        "botComparison": {
            "classification": bot_class,
            "trade": (
                {"strategy": actual_strategy}
                if actual_strategy
                else None
            ),
        },
    }


def interaction(
    *,
    state="continuation",
    classification="hypothesis_first",
):
    return {
        "strategy": "trend_structure",
        "state": state,
        "hypothesisSide": "long",
        "forward": {
            "120s": {
                "movementBands": {
                    "0.20%": {
                        "classification": classification,
                    }
                }
            }
        },
    }


def report(
    *,
    session_file,
    run_label,
    start_ts,
    end_ts,
    trades=None,
    opportunities=None,
    interactions=None,
):
    trades = trades or []
    opportunities = opportunities or []
    interactions = interactions or []
    return {
        "schemaVersion": 1,
        "generatedAt": "2026-09-23T00:00:00+00:00",
        "session": {
            "file": session_file,
            "eventCount": 100,
        },
        "runSummary": {
            "runLabel": run_label,
            "closedTrades": len(trades),
            "realizedPnl": sum(
                row.get("netPnl", 0.0)
                for row in trades
            ),
        },
        "postRunOpportunity": {
            "hindsight": {
                "policy": {
                    "runStartTs": start_ts,
                    "runEndTs": end_ts,
                },
                "summary": {
                    "opportunities": len(opportunities),
                },
                "opportunities": opportunities,
            }
        },
        "strategySideRegimePerformance": {
            "summary": {
                "closedTrades": len(trades),
            },
            "trades": trades,
        },
        "marketInteractionResearch": {
            "summary": {
                "checkpoints": len(interactions),
            },
            "checkpoints": interactions,
        },
    }


def write_report(path, payload):
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def write_pack(
    path,
    *,
    payload,
    source_file,
    rows=None,
):
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            "session-report.json",
            json.dumps(payload),
        )
        archive.writestr(
            "manifest.json",
            json.dumps({
                "source": {
                    "file": source_file,
                }
            }),
        )
        if rows is not None:
            archive.writestr(
                "session-analysis.jsonl",
                "\n".join(
                    json.dumps(row)
                    for row in rows
                )
                + "\n",
            )


def test_duplicate_report_and_pack_for_same_session_are_counted_once(tmp_path):
    payload = report(
        session_file="session-a.jsonl",
        run_label="run-a",
        start_ts=10.0,
        end_ts=20.0,
        trades=[trade()],
        opportunities=[opportunity()],
    )
    report_path = tmp_path / "session-a-report.json"
    pack_path = tmp_path / "session-a-analysis-pack.zip"
    write_report(report_path, payload)
    write_pack(
        pack_path,
        payload=payload,
        source_file="session-a.jsonl",
    )

    dataset = aggregate_research_sources(
        [report_path, pack_path],
        minimum_group_samples=1,
        minimum_segment_samples=1,
    )

    assert dataset["summary"]["sessions"] == 1
    assert dataset["summary"]["duplicateSourcesIgnored"] == 1
    assert dataset["summary"]["closedTrades"] == 1
    assert dataset["summary"]["hindsightOpportunities"] == 1
    assert len(dataset["duplicates"]) == 1


def test_cross_session_trade_features_and_calibration_pool_sessions(tmp_path):
    first = report(
        session_file="session-a.jsonl",
        run_label="run-a",
        start_ts=10.0,
        end_ts=20.0,
        trades=[
            trade(
                net=-2.0,
                realized_all_in_r=-0.4,
                flow="short_term_reversal",
                freshness="late",
                liquidity="opposed",
                rr=0.9,
            )
        ],
    )
    second = report(
        session_file="session-b.jsonl",
        run_label="run-b",
        start_ts=30.0,
        end_ts=40.0,
        trades=[
            trade(
                net=3.0,
                realized_all_in_r=0.6,
                flow="strongly_aligned",
                freshness="fresh",
                liquidity="supportive",
                rr=1.3,
            )
        ],
    )
    first_path = tmp_path / "a.json"
    second_path = tmp_path / "b.json"
    write_report(first_path, first)
    write_report(second_path, second)

    dataset = aggregate_research_sources(
        [first_path, second_path],
        minimum_group_samples=2,
        minimum_segment_samples=1,
    )

    assert dataset["summary"]["sessions"] == 2
    assert dataset["summary"]["closedTrades"] == 2

    feature = {
        (
            row["dimension"],
            row["value"],
        ): row
        for row in dataset["tradeFeatureOutcomes"]
        if (
            row["strategy"] == "trend_structure"
            and row["side"] == "long"
            and row["regime"] == "bullish_trend"
        )
    }
    assert feature[
        ("flowAlignment", "short_term_reversal")
    ]["netPnl"] == pytest.approx(-2.0)
    assert feature[
        ("flowAlignment", "strongly_aligned")
    ]["netPnl"] == pytest.approx(3.0)

    calibration = dataset[
        "conditionalEconomicCalibration"
    ]
    assert calibration["summary"]["economicTrades"] == 2
    group = calibration["groups"][0]
    assert group["sampleReady"] is True
    assert group["baseline"]["samples"] == 2


def test_hindsight_coverage_is_recomputed_across_sessions(tmp_path):
    first = report(
        session_file="session-a.jsonl",
        run_label="run-a",
        start_ts=10.0,
        end_ts=20.0,
        opportunities=[
            opportunity(
                opportunity_id="h1",
                fit="observed_aligned",
                bot_class="missed",
            )
        ],
    )
    second = report(
        session_file="session-b.jsonl",
        run_label="run-b",
        start_ts=30.0,
        end_ts=40.0,
        opportunities=[
            opportunity(
                opportunity_id="h2",
                fit="tradeable_aligned",
                bot_class="traded",
                actual_strategy="trend_structure",
            )
        ],
    )
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    write_report(a, first)
    write_report(b, second)

    dataset = aggregate_research_sources(
        [a, b],
    )

    row = next(
        value
        for value in dataset["hindsightCoverage"]
        if (
            value["strategy"] == "trend_structure"
            and value["side"] == "long"
            and value["regime"] == "bullish_trend"
        )
    )
    assert row["opportunities"] == 2
    assert row["sessions"] == 2
    assert row["observed"] == 2
    assert row["tradeable"] == 1
    assert row["traded"] == 1
    assert row["tradeCoverageRate"] == pytest.approx(0.5)
    # Density is evidence-only and must not appear as a playbook.
    assert all(
        value["strategy"] != "orderbook_density"
        for value in dataset["hindsightCoverage"]
    )


def test_pack_rows_preserve_arbiter_blocks_when_source_contains_analysis_rows(tmp_path):
    payload = report(
        session_file="session-a.jsonl",
        run_label="run-a",
        start_ts=10.0,
        end_ts=20.0,
    )
    pack = tmp_path / "session-a-analysis-pack.zip"
    write_pack(
        pack,
        payload=payload,
        source_file="session-a.jsonl",
        rows=[
            {
                "ts": 12.0,
                "event": "arbiter_blocked",
                "symbol": "AAAUSDT",
                "payload": {
                    "strategy": "trend_structure",
                    "setupId": "setup-1",
                    "blockers": [
                        "mature_structural_obstacle_before_first_take"
                    ],
                    "conflictingStrategies": [],
                },
            }
        ],
    )

    dataset = aggregate_research_sources([pack])

    assert dataset["summary"]["arbiterBlocks"] == 1
    assert dataset["arbiterBlockSummary"] == [
        {
            "strategy": "trend_structure",
            "blocker": (
                "mature_structural_obstacle_before_first_take"
            ),
            "count": 1,
            "sessions": 1,
        }
    ]


def test_interaction_outcomes_keep_horizon_band_and_session_count(tmp_path):
    first = report(
        session_file="session-a.jsonl",
        run_label="run-a",
        start_ts=10.0,
        end_ts=20.0,
        interactions=[
            interaction(
                classification="hypothesis_first"
            )
        ],
    )
    second = report(
        session_file="session-b.jsonl",
        run_label="run-b",
        start_ts=30.0,
        end_ts=40.0,
        interactions=[
            interaction(
                classification="opposite_first"
            )
        ],
    )
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    write_report(a, first)
    write_report(b, second)

    dataset = aggregate_research_sources([a, b])

    rows = [
        row
        for row in dataset["marketInteractionOutcomes"]
        if (
            row["strategy"] == "trend_structure"
            and row["state"] == "continuation"
            and row["horizon"] == "120s"
            and row["band"] == "0.20%"
        )
    ]
    counts = {
        row["classification"]: row["count"]
        for row in rows
    }
    assert counts == {
        "hypothesis_first": 1,
        "opposite_first": 1,
    }


def test_research_pack_contains_normalized_tables_and_report(tmp_path):
    payload = report(
        session_file="session-a.jsonl",
        run_label="run-a",
        start_ts=10.0,
        end_ts=20.0,
        trades=[trade()],
        opportunities=[opportunity()],
        interactions=[interaction()],
    )
    source = tmp_path / "session-a-report.json"
    write_report(source, payload)
    output = tmp_path / "dataset.zip"

    result = write_research_dataset_pack(
        [source],
        output_path=output,
        minimum_group_samples=1,
        minimum_segment_samples=1,
    )

    assert result == output
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "arbiter-blocks.jsonl",
            "cross-session-report.json",
            "hindsight-opportunities.jsonl",
            "manifest.json",
            "market-interactions.jsonl",
            "sessions.jsonl",
            "trades.jsonl",
        }
        cross = json.loads(
            archive.read(
                "cross-session-report.json"
            )
        )
        manifest = json.loads(
            archive.read("manifest.json")
        )
        trades = [
            json.loads(line)
            for line in archive.read(
                "trades.jsonl"
            ).decode("utf-8").splitlines()
            if line.strip()
        ]

    assert cross["summary"]["sessions"] == 1
    assert manifest["summary"]["closedTrades"] == 1
    assert trades[0]["sessionId"] == cross["sessions"][0]["sessionId"]
