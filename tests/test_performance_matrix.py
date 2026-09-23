import pytest

from scalp_bot.performance_matrix import (
    build_strategy_side_regime_report,
    pair_closed_trades,
)


def opened(
    ts: float,
    *,
    symbol: str,
    strategy: str,
    side: str,
    setup_id: str,
    regime: str,
    net_rr: float,
    freshness: str,
    spent: float,
    flow: str,
    liquidity: str,
    confluence: int = 0,
) -> dict:
    return {
        "ts": ts,
        "event": "trade_opened",
        "symbol": symbol,
        "payload": {
            "plan": {
                "strategy": strategy,
                "side": side,
                "setup_id": setup_id,
                "market_entry": 100.0,
                "net_reward_risk": net_rr,
                "strategy_details": {
                    "decisionContext": {
                        "localRegime": regime,
                        "htfBias": "neutral",
                        "executionReady": True,
                    },
                    "entryFreshness": {
                        "classification": freshness,
                        "moveSpentRatio": spent,
                        "confirmationAgeSeconds": 5.0,
                    },
                    "flowAlignment": {
                        "classification": flow,
                        "score": 0.5,
                    },
                    "liquidityAlignment": {
                        "classification": liquidity,
                        "score": 0.5,
                    },
                    "semanticArbitration": {
                        "confluenceCount": confluence,
                    },
                },
            },
            "position": {
                "side": side,
                "entry": 100.0,
                "setup_id": setup_id,
            },
        },
    }


def closed(
    ts: float,
    *,
    symbol: str,
    strategy: str,
    side: str,
    setup_id: str,
    gross: float,
    fees: float,
    net: float,
    initial_risk: float,
    mfe_r: float,
    mae_r: float,
    partial: bool = False,
    reason: str = "stop",
) -> dict:
    return {
        "ts": ts,
        "event": "trade_closed",
        "symbol": symbol,
        "payload": {
            "strategy": strategy,
            "side": side,
            "setupId": setup_id,
            "entry": 100.0,
            "exit": 100.2,
            "grossPnl": gross,
            "fees": fees,
            "netPnl": net,
            "initialRiskUsd": initial_risk,
            "mfeR": mfe_r,
            "maeR": mae_r,
            "maxFavorableMoveBps": mfe_r * 50,
            "maxAdverseMoveBps": mae_r * 50,
            "partialTaken": partial,
            "reason": reason,
        },
    }


def hindsight_fixture() -> dict:
    return {
        "opportunities": [
            {
                "side": "long",
                "marketSnapshots": {
                    "oracleEntry": {
                        "marketContext": {
                            "localRegime": {
                                "regime": "bullish_trend"
                            }
                        }
                    }
                },
                "strategyFit": {
                    "strategies": [
                        {
                            "strategy": "trend_structure",
                            "fit": "observed_aligned",
                        },
                        {
                            "strategy": "level_breakout",
                            "fit": "unaware",
                        },
                    ]
                },
                "botComparison": {
                    "classification": "missed",
                    "trade": None,
                },
            },
            {
                "side": "short",
                "marketSnapshots": {
                    "oracleEntry": {
                        "marketContext": {
                            "localRegime": {
                                "regime": "bearish_trend"
                            }
                        }
                    }
                },
                "strategyFit": {
                    "strategies": [
                        {
                            "strategy": "trend_structure",
                            "fit": "tradeable_aligned",
                        }
                    ]
                },
                "botComparison": {
                    "classification": "traded",
                    "trade": {
                        "strategy": "trend_structure",
                    },
                },
            },
        ]
    }


def test_pair_closed_trades_preserves_entry_context_and_realized_r() -> None:
    rows = [
        opened(
            10.0,
            symbol="AAAUSDT",
            strategy="trend_structure",
            side="long",
            setup_id="long-1",
            regime="bullish_trend",
            net_rr=0.9,
            freshness="late",
            spent=0.62,
            flow="short_term_reversal",
            liquidity="opposed",
        ),
        closed(
            50.0,
            symbol="AAAUSDT",
            strategy="trend_structure",
            side="long",
            setup_id="long-1",
            gross=-4.0,
            fees=1.0,
            net=-5.0,
            initial_risk=5.0,
            mfe_r=0.1,
            mae_r=0.9,
        ),
    ]

    trades = pair_closed_trades(rows)

    assert len(trades) == 1
    trade = trades[0]
    assert trade["regime"] == "bullish_trend"
    assert trade["plannedNetRewardRisk"] == pytest.approx(0.9)
    assert trade["realizedR"] == pytest.approx(-1.0)
    assert trade["moveSpentRatio"] == pytest.approx(0.62)
    assert trade["flowAlignmentClass"] == "short_term_reversal"
    assert trade["liquidityAlignmentClass"] == "opposed"


def test_report_separates_long_short_and_regimes() -> None:
    rows = [
        opened(
            10.0,
            symbol="AAAUSDT",
            strategy="trend_structure",
            side="long",
            setup_id="long-1",
            regime="bullish_trend",
            net_rr=0.9,
            freshness="late",
            spent=0.62,
            flow="short_term_reversal",
            liquidity="opposed",
        ),
        closed(
            50.0,
            symbol="AAAUSDT",
            strategy="trend_structure",
            side="long",
            setup_id="long-1",
            gross=-4.0,
            fees=1.0,
            net=-5.0,
            initial_risk=5.0,
            mfe_r=0.1,
            mae_r=0.9,
        ),
        opened(
            60.0,
            symbol="BBBUSDT",
            strategy="trend_structure",
            side="short",
            setup_id="short-1",
            regime="bearish_trend",
            net_rr=0.8,
            freshness="fresh",
            spent=0.1,
            flow="strongly_aligned",
            liquidity="supportive",
            confluence=1,
        ),
        closed(
            100.0,
            symbol="BBBUSDT",
            strategy="trend_structure",
            side="short",
            setup_id="short-1",
            gross=8.0,
            fees=1.0,
            net=7.0,
            initial_risk=5.0,
            mfe_r=1.8,
            mae_r=0.2,
            partial=True,
            reason="target",
        ),
    ]

    report = build_strategy_side_regime_report(rows)

    assert report["summary"]["closedTrades"] == 2
    assert report["summary"]["longTrades"] == 1
    assert report["summary"]["shortTrades"] == 1

    side = {
        row["side"]: row
        for row in report["sideSummary"]
    }
    assert side["long"]["netPnl"] == pytest.approx(-5.0)
    assert side["long"]["winRate"] == 0
    assert side["short"]["netPnl"] == pytest.approx(7.0)
    assert side["short"]["winRate"] == 1
    assert side["short"]["averageMfeR"] == pytest.approx(1.8)

    matrix = {
        (row["strategy"], row["side"], row["regime"]): row
        for row in report["byStrategySideRegime"]
    }
    long_row = matrix[
        ("trend_structure", "long", "bullish_trend")
    ]
    short_row = matrix[
        ("trend_structure", "short", "bearish_trend")
    ]
    assert long_row["flowAlignmentCounts"] == {
        "short_term_reversal": 1
    }
    assert long_row["averageEntryMoveSpentRatio"] == pytest.approx(
        0.62
    )
    assert short_row["flowAlignmentCounts"] == {
        "strongly_aligned": 1
    }
    assert short_row["confluenceCounts"] == {"1": 1}
    assert short_row["partialTakeRate"] == 1.0


def test_hindsight_matrix_tracks_observed_tradeable_and_traded_coverage() -> None:
    report = build_strategy_side_regime_report(
        [],
        hindsight=hindsight_fixture(),
    )

    matrix = {
        (row["strategy"], row["side"], row["regime"]): row
        for row in report["hindsightByStrategySideRegime"]
    }
    long_trend = matrix[
        ("trend_structure", "long", "bullish_trend")
    ]
    short_trend = matrix[
        ("trend_structure", "short", "bearish_trend")
    ]
    breakout_long = matrix[
        ("level_breakout", "long", "bullish_trend")
    ]

    assert long_trend["opportunities"] == 1
    assert long_trend["observed"] == 1
    assert long_trend["tradeable"] == 0
    assert long_trend["traded"] == 0
    assert long_trend["observedCoverageRate"] == 1.0

    assert short_trend["opportunities"] == 1
    assert short_trend["tradeable"] == 1
    assert short_trend["traded"] == 1
    assert short_trend["tradeCoverageRate"] == 1.0

    assert breakout_long["opportunities"] == 1
    assert breakout_long["observed"] == 0
    assert breakout_long["fitCounts"] == {"unaware": 1}


def test_unknown_entry_regime_is_retained_not_dropped() -> None:
    rows = [
        {
            "ts": 10.0,
            "event": "trade_opened",
            "symbol": "AAAUSDT",
            "payload": {
                "plan": {
                    "strategy": "level_breakout",
                    "side": "long",
                    "setup_id": "legacy",
                    "market_entry": 100.0,
                    "strategy_details": {},
                }
            },
        },
        closed(
            20.0,
            symbol="AAAUSDT",
            strategy="level_breakout",
            side="long",
            setup_id="legacy",
            gross=1.0,
            fees=0.2,
            net=0.8,
            initial_risk=1.0,
            mfe_r=1.0,
            mae_r=0.2,
        ),
    ]

    report = build_strategy_side_regime_report(rows)

    assert report["byStrategySideRegime"][0]["regime"] == "unknown"
