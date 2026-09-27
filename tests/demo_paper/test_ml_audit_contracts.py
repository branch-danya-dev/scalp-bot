"""Immutable V2 metadata and visibility of rejected delivered forecasts."""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
import time
import pytest
from .test_contracts import make_pair
from .test_integration import metadata
from scalp_bot.demo_paper.contracts import SafetyError
from scalp_bot.demo_paper.engine import PairedEngine
from scalp_bot.demo_paper.ml_adapter import ResearchMLAdapter
from scalp_bot.demo_paper.preflight import ROOT
from scalp_bot.ml.contracts import SnapshotRef, ImpulseForecast
from scalp_bot.ml.features import FEATURE_SCHEMA
from scalp_bot.domain import OrderBook


def test_preflight_pins_calibration_manifest_alongside_weights(tmp_path,monkeypatch):
    import scalp_bot.demo_paper.preflight as pf
    model=tmp_path/'model.cbm';model.write_bytes(b'synthetic model identity')
    model_sha=hashlib.sha256(model.read_bytes()).hexdigest()
    m=metadata();m['model_sha256']=model_sha
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps(m))
    monkeypatch.setattr(pf,'MODEL_SHA',model_sha)
    monkeypatch.setattr(pf,'MODEL_MANIFEST_SHA',hashlib.sha256(manifest.read_bytes()).hexdigest(),raising=False)
    pf.local_preflight(ROOT/'docs/demo-paper-1h/passport.json',tmp_path,tmp_path/'output')
    m['calibration']={'tampered':'different calibration with identical weights'}
    manifest.write_text(json.dumps(m))
    with pytest.raises(SafetyError,match='manifest'):
        pf.local_preflight(ROOT/'docs/demo-paper-1h/passport.json',tmp_path,tmp_path/'output')


@pytest.mark.parametrize('blocked',['clock','inactive','instrument'])
def test_delivered_but_inadmissible_forecast_stays_in_decision_funnel(blocked):
    portfolio,arms,spec,events=make_pair();now=time.perf_counter_ns();m=metadata()
    source=SnapshotRef('r','BTCUSDT',1,1,1000,now,'perf_counter',FEATURE_SCHEMA)
    forecast=ImpulseForecast(source,m['model_version'],m['policy_version'],'long',30000,now+1,now+1_000_000_000,.6,.2,.2)
    if blocked=='instrument':spec=replace(spec,min_notional_value=1000)
    session=SimpleNamespace(symbol="BTCUSDT",instrument=spec,book_is_fresh=lambda:True,deep_book_is_fresh=lambda:True,
        decisions={},orderbook=OrderBook(bids=[(99.99,100)],asks=[(100,100)]))
    engine=PairedEngine.__new__(PairedEngine)
    engine.worker=SimpleNamespace(poll=lambda:[('forecast',forecast,100)],failed=None,ready=False)
    engine.sessions={} if blocked=='inactive' else {'BTCUSDT':session}
    engine.portfolio=portfolio;engine.adapter=ResearchMLAdapter(m,portfolio,portfolio.emit)
    engine.ml_current={'BTCUSDT':source};engine.ml_quotes={('BTCUSDT',1,'long'):100}
    engine._clock_entry_block=lambda s:blocked=='clock'
    engine.poll_ml()
    decisions=[p for name,p in events if name=='ml_decision']
    assert len(decisions)==1 and not portfolio.reservations
    expected={'clock':'market_clock_invalid','inactive':'symbol_inactive','instrument':'instrument_constraints'}[blocked]
    assert expected in decisions[0]['reasons']

