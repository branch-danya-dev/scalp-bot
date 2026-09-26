"""UI rendering contract without a server, exchange or browser network."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_router_panel_renders_roles_rejection_sizing_and_escapes_values():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for this static UI rendering check')
    source=(Path(__file__).resolve().parents[1]/'scalp_bot/static/app.js').read_text(encoding='utf-8')
    render=source[source.index('function renderScenarioRouting('):source.index('function renderDecisions(')]
    fixture={'situation':{'status':'OBSERVING','strategies':{
        'level_breakout':{'status':'applicable'},'weak_level_rejection':{'status':'not_applicable'},
        'trend_structure':{'status':'disabled'},'orderbook_density':{'status':'evidence_only'},
        'price_action_hypothesis':{'status':'disabled'}}},
        'scenario':{'state':'ARMED','owner':'level_breakout','side':'long','scenarioId':'<script>',
            'reasons':['structural boundary'],'entryArea':[100,101],
            'lastRejection':{'owner':'risk','reason':'insufficient <capital>'}}}
    code='''const assert=require('node:assert/strict'); const panel={innerHTML:''};
    const $=()=>panel; const strategyLabel=x=>x; const translatePhrase=x=>x;
    const localRegimeLabel=x=>x; const sideLabel=x=>x; const price=x=>String(x); const money=x=>String(x);
    '''+render+'\nrenderScenarioRouting('+json.dumps(fixture)+''',null);
    assert(panel.innerHTML.includes('План готов'));
    assert(panel.innerHTML.includes('выключена'));
    assert(panel.innerHTML.includes('подтверждение, без сделок'));
    assert(panel.innerHTML.includes('Последний отказ · риск'));
    assert(panel.innerHTML.includes('&lt;script&gt;'));
    assert(!panel.innerHTML.includes('<script>'));
    renderScenarioRouting(null,null); assert.equal(panel.innerHTML,'');'''
    subprocess.run([node,'-e',code],check=True,capture_output=True,text=True,timeout=10)
