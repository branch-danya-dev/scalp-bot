import pytest
from scalp_bot.domain import TradeTick
from scalp_bot.config import Settings
from scalp_bot.strategy.rejection_response import assess_reclaim_response


@pytest.mark.parametrize("sign",[1,-1])
def test_response_requires_both_channels_after_reclaim_and_ignores_future(sign):
    side="long" if sign==1 else "short"
    old=[TradeTick(999,50,1,"Buy"),TradeTick(1000,100,1,"Buy")]
    fresh=[TradeTick(1001,100,1,"Buy"),TradeTick(1200,100+sign*.02,1,"Buy")]
    future=[TradeTick(1400,100-sign*10,1,"Sell")]
    result=assess_reclaim_response(side,1000,100,100+sign*.02,old+fresh+future,1300)
    assert result["allowed"] and result["tradeCount"]==2
    assert not assess_reclaim_response(side,1000,100,100-sign*.02,old+fresh,1300)["allowed"]
    assert not assess_reclaim_response(side,1000,100,100+sign*.02,old,1300)["allowed"]


def test_research_policy_is_disabled_by_default():
    assert Settings(_env_file=None).research_rejection_response_policy=="legacy"
