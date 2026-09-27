import json
from types import SimpleNamespace
import pytest
from scalp_bot.domain import OrderBook
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.ml.dataset import PendingLabel,POLICY_PATH
pytest.importorskip("numpy")
from scalp_bot.ml.plan_diagnosis import fixed_rank_slices,state_groups,summaries
from scalp_bot.ml.features import FEATURE_NAMES

@pytest.mark.parametrize('side',['long','short'])
@pytest.mark.parametrize('move_bps,profitable',[(0,False),(20,True)])
def test_timeout_is_actual_net_including_two_fills(side,move_bps,profitable):
    policy=json.loads(POLICY_PATH.read_text());sign=1 if side=='long' else -1
    instrument=InstrumentSpec('X','Trading',.001,.001,.001,1,100000,100000,480,20)
    a=SimpleNamespace(epoch=0,health_reason=None,instrument=instrument,session=SimpleNamespace(orderbook=OrderBook([(100,1000)],[(100.001,1000)])))
    row=dict(ref={'available_mono_ns':0},side=side);p=PendingLabel(row,0,30_000_000_000,100_000_000)
    assert p.advance(a,100_000_000,policy) is None
    price=p.entry*(1+sign*move_bps/10000)
    a.session.orderbook=OrderBook([(price,1000)],[(price+.001,1000)])
    assert p.advance(a,30_000_000_000,policy)=='timeout'
    assert (row['net_usdt']>0)==profitable
    assert row['gross_usdt']==pytest.approx(sign*(row['exit']-row['entry'])*row['quantity'])
    assert row['fees_usdt']==pytest.approx((row['exit']+row['entry'])*row['quantity']*.00055)
    assert row['net_usdt']==pytest.approx(row['gross_usdt']-row['fees_usdt'])

def test_fixed_groups_do_not_depend_on_outcome():
    f=[None]*len(FEATURE_NAMES)
    for k,v in dict(liquidity_known=0,micro_move_5s_bps=-2,trade_imbalance_5s=-.2,range_bps=15,spread_bps=3,top5_depth_usd=1000).items():f[FEATURE_NAMES.index(k)]=v
    r=dict(side='short',features=f,label='stop_first');before=state_groups(r);r['label']='target_first'
    assert before==state_groups(r)
    assert before==dict(liquidity='unknown',signed_move5='high',signed_flow5='high',range='medium',spread='high',depth='medium')

def test_rank_slices_overlap_and_threshold_is_unchanged():
    np=pytest.importorskip('numpy');s=dict(fixed_rank_slices(np.arange(100)/1000))
    assert set(s['top_1pct'])<=set(s['top_5pct'])<=set(s['top_10pct'])
    assert len(s['threshold_0.55'])==0
    assert sorted(np.concatenate([s[f'decile_{i:02d}'] for i in range(1,11)]))==list(range(100))
