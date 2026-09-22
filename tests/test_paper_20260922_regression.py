import json
from pathlib import Path

import pytest

from scalp_bot.domain import Action
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.density import DensityBounceStrategy
from scalp_bot.strategy.liquidity import LiquidityTarget


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "paper_20260922_regressions.json"
)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_archived_density_cases_were_not_entry_confirmed() -> None:
    data = load_fixture()

    assert len(data["densityCases"]) == 10
    for case in data["densityCases"]:
        status = DensityBounceStrategy._reaction_confirmation(
            case["side"],
            case["mid"],
            case["wallPrice"],
            participation_confirmed=case["participationConfirmed"],
            recent_trade_count=case["recentTradeCount"],
            recent_imbalance=case["recentImbalance"],
        )
        assert status["priceReactionConfirmed"] is case["expectedPriceReaction"]
        assert status["flowReversalConfirmed"] is case["expectedFlowReversal"]
        assert status["entryConfirmationComplete"] is False
        assert case["nextState"] == "exhausted"


def test_near_good_breakout_nearest_liquidity_is_obstacle_not_final_target() -> None:
    case = load_fixture()["breakoutCases"][0]
    risk = abs(case["entry"] - case["stop"])
    nearest = LiquidityTarget(
        price=case["oldNearestTarget"],
        kind="previous_day_high",
        touches=1,
        score=5.0,
    )

    (
        target,
        obstacle,
        selected_liquidity,
        target_r,
    ) = LevelBreakoutStrategy._select_breakout_target(
        case["entry"],
        risk,
        Action.LONG,
        expected_impulse=0.0,
        liquidity_ladder=[nearest],
        minimum_target_r=1.25,
    )

    assert obstacle is nearest
    assert selected_liquidity is None
    assert target > case["oldNearestTarget"]
    assert target_r == pytest.approx(1.25)


def test_near_archive_preserves_good_and_bad_signal_difference() -> None:
    good, bad = load_fixture()["breakoutCases"]

    assert good["reviewLabel"] == "target_first"
    assert bad["reviewLabel"] == "stop_first"
    assert good["setupQuality"] > bad["setupQuality"]
    assert good["pressureScore"] > bad["pressureScore"]
    assert good["flowImbalance5s"] > bad["flowImbalance5s"]
    assert good["oldTargetDelaySeconds"] is not None
    assert bad["oldStopDelaySeconds"] is not None
