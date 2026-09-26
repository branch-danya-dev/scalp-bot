from copy import deepcopy
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import zipfile
import hashlib

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import OrderBook, Trend
from scalp_bot.e06_capture import load_profiles, settings, verify_smoke
from scalp_bot.manifest_validation import check_manifest, fingerprint
from scalp_bot.strategy.breakout import LevelBreakoutStrategy
from scalp_bot.strategy.common import compute_trade_flow
from scalp_bot.strategy.flow import LevelFlow
from test_e01_live import prepared_smoke
from test_strategy_level_semantics import mature_breakout_candles, mature_structure, aggressive_buy_flow


@pytest.mark.parametrize('short',[False,True])
def test_native_matches_research_overlay_across_structure_loss_and_new_episode(monkeypatch,short):
    import scalp_bot.strategy.breakout as module
    sign=-1 if short else 1
    flow=LevelFlow(buy_notional=0 if short else 10000,sell_notional=10000 if short else 0,
        total_notional=10000,imbalance=sign,trade_count=12,price_response_pct=sign*.0007,absorption_efficiency=.05)
    monkeypatch.setattr(module,'flow_at_level',lambda *a,**k:flow)
    monkeypatch.setattr(module,'flow_beyond_level',lambda *a,**k:flow)
    native,reference=LevelBreakoutStrategy(),LevelBreakoutStrategy()
    native.conditional_hold_enabled=True
    pressure={'nearCloses':2,'compressedPullbacks':2}
    for s in (native,reference): s._pressure_score=lambda *a,**k:(4,dict(pressure))
    overlay=runpy.run_path('scripts/compare-conditional-breakout-hold.py')['install_overlay']
    overlay(reference,NS(perf_counter_ns=lambda:1),StringIO(),True)
    candles=mature_breakout_candles(); structure=mature_structure(); ticks=aggressive_buy_flow()
    if short:
        for c in candles: c.open,c.high,c.low,c.close=200-c.open,200-c.low,200-c.high,200-c.close
        level=structure.levels[0]
        level.kind='support'; level.low,level.high=200-level.high,200-level.low
        level.level_id='S:100';level.generation_id='S:100:g1'
        for t in ticks: t.price=200-t.price;t.side='Sell' if t.side=='Buy' else 'Buy'
    trade_flow=compute_trade_flow(ticks)
    trade_flow['participationConfirmed']=True
    actions=[]
    for seconds,near,compressed,mid in [(0,2,2,100.165),(2,2,2,100.165),(3.2,0,0,100.165),
                                       (4,2,2,100.165),(5,2,2,99.99),(6,2,2,100.165),(10,2,2,100.165)]:
        pressure.update(nearCloses=near,compressedPullbacks=compressed)
        price=200-mid if short else mid
        book=OrderBook(bids=[(price-.005,50)],asks=[(price+.005,50)])
        kwargs=dict(symbol='TEST',trades=ticks,structure=structure,observed_at_ms=30_004_600+int(seconds*1000),trade_flow=trade_flow)
        a=native.evaluate(candles,book,Trend.DOWN if short else Trend.UP,**kwargs)
        b=reference.evaluate(candles,book,Trend.DOWN if short else Trend.UP,**kwargs)
        assert a==b and native._states==reference._states
        assert native.hold_without_retest_seconds==reference.hold_without_retest_seconds==8
        actions.append(a.action.value)
    assert actions[2]=='wait'
    assert actions[3]==('short' if short else 'long')


def test_profiles_default_off_and_historical_manifest_v3(monkeypatch):
    monkeypatch.setenv('SCALP_E06_CONDITIONAL_BREAKOUT_HOLD','true')
    profiles=load_profiles(Path.cwd())
    assert all(not c.e06_conditional_breakout_hold for c in profiles.values())
    assert profiles['independent'].paper_run_duration_seconds==43200
    for bad in ({'e01_breakout_obstacle_veto':True},{'e06_conditional_breakout_hold':True},
                {'paper_run_duration_seconds':43000},{'breakout_hold_without_retest_seconds':3}):
        profile=json.loads(Path('configs/e06-independent-12h.json').read_text())
        profile.update(bad)
        with pytest.raises(ValueError): settings(profile,'independent')
    original=json.loads(Path('data/e01-smoke-03/result.json').read_text(encoding='utf-8')) if Path('data/e01-smoke-03/result.json').exists() else None
    if original: assert not check_manifest(original['portfolios']['baseline']['manifest'])[1]


@pytest.mark.asyncio
async def test_e06_capture_audit_replay_and_archive(tmp_path,monkeypatch):
    runner,_=await prepared_smoke(tmp_path,monkeypatch)
    runner.experiment='E06'
    result=await runner.run()
    assert result['experiment']=='E06'
    a,b=(result['portfolios'][n]['manifest'] for n in ('baseline','candidate'))
    assert a['manifestVersion']==b['manifestVersion']==4
    assert {k for k in a['config'] if a['config'][k]!=b['config'][k]}=={'e06_conditional_breakout_hold'}
    assert not a['config']['e01_breakout_obstacle_veto'] and not b['config']['e01_breakout_obstacle_veto']
    assert not runner.pair.engines['baseline'].strategies['level_breakout'].conditional_hold_enabled
    assert runner.pair.engines['candidate'].strategies['level_breakout'].conditional_hold_enabled
    audit=runpy.run_path('scripts/audit-e01-capture.py')['audit']
    assert audit(runner.directory)['experiment']=='E06'
    with zipfile.ZipFile(runner.directory/'source-at-capture.zip') as archive:
        assert set(archive.namelist())==set(a['code']['fileHashes'])
        assert all(hashlib.sha256(archive.read(p)).hexdigest()==h for p,h in a['code']['fileHashes'].items())
    replay=runpy.run_path('scripts/replay-paired-capture.py')['run']
    proof=await replay(runner.directory,tmp_path/'replay')
    assert proof['status']=='paired_full_replay_matched' and proof['experiment']=='E06'
    assert proof['inputs']==result['eventsConsumed']
    # A completed but short fixture is not permission to start the real 12h capture.
    with pytest.raises(ValueError,match='profile differs'):
        verify_smoke(runner.directory,load_profiles(Path.cwd())['smoke'],audit)


@pytest.mark.asyncio
async def test_default_replay_preserves_collector_import_path(tmp_path,monkeypatch):
    replay=runpy.run_path('scripts/replay-paired-capture.py')['run']
    # Model a script entry point without the editable repository root on sys.path.
    paths=[p for p in sys.path if Path(p).resolve()!=Path.cwd()]
    monkeypatch.setattr(sys,'path',paths[:])
    with pytest.raises(FileNotFoundError):
        await replay(tmp_path/'missing',tmp_path/'output')
    assert sys.path==paths
