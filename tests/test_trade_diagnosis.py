import sqlite3
import pytest
from scalp_bot.offline_trade_diagnosis import path_stats,reconcile

def test_quote_causal_fence_and_stop_before_later_target():
    db=sqlite3.connect(':memory:');db.execute('create table quotes(symbol,mono,seq,bid,ask)')
    db.executemany('insert into quotes values(?,?,?,?,?)',[('X',0,1,100,100.01),('X',0,2,80,80.01),('X',1,3,101,101.01)])
    p=path_stats(db,'X','long',0,1,entry=100,stop=99,target=100.5,horizon=1)
    assert p['entry']==100
    assert p['first_barrier']=='stop' and p['target_first_s']==1
    assert p['observed_mae_bps']==-2000

def test_quote_gap_cannot_be_claimed_complete():
    db=sqlite3.connect(':memory:');db.execute('create table quotes(symbol,mono,seq,bid,ask)')
    db.executemany('insert into quotes values(?,?,?,?,?)',[('X',0,1,100,100.01),('X',2,2,101,101.01)])
    p=path_stats(db,'X','short',0,1,horizon=2)
    assert p['status']=='quote_gap' and 'markout_bps' not in p

def test_delta_reconciles_size_and_changed_outcome_without_inventing_match():
    def t(episode,q,net):return dict(symbol='X',originalQuantity=q,netPnl=net,openedAt=1,strategyDetails={'scenario':dict(owner='s',objectRef={'key':'level'},episodeKey=episode,side='long')})
    rows,total=reconcile(dict(A=[t('a',2,4),t('gone',1,-3)],B=[t('a',3,9),t('new',1,-7)]))
    assert total['arithmetic_residual']==0 and total['expected_delta']==1
    retained=next(r for r in rows if r['effect']=='retained')
    assert retained['quantity_contribution_usdt']==2 and retained['per_unit_path_contribution_usdt']==3
