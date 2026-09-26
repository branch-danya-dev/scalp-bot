import json
from pathlib import Path
import runpy
from types import SimpleNamespace as NS

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('replay_failure',[False,True])
async def test_independent_phase_requires_completed_smoke_audit_and_replay(tmp_path,monkeypatch,replay_failure):
    script=runpy.run_path('scripts/run-e06-paper.py')
    scope=script['run'].__globals__
    events=[]
    class Capture:
        def __init__(self,config,folder,**kwargs):
            self.folder=folder
            self.phase='smoke' if config.paper_run_duration_seconds==1800 else 'independent'
            assert kwargs['experiment']=='E06'
        async def run(self):
            events.append('capture:'+self.phase)
            self.folder.mkdir()
            return {'eventsConsumed':1}
    def audit(folder):
        events.append('audit:'+folder.name)
        return {'status':'passed'}
    def verify(folder,config,auditor):
        return auditor(folder)
    async def replay(folder,output):
        events.append('replay:'+folder.name)
        if replay_failure: raise ValueError('replay mismatch')
        output.mkdir()
        return {'status':'paired_full_replay_matched'}
    def load(path):
        return {'audit':audit} if Path(path).name=='audit-e01-capture.py' else {'run':replay}
    monkeypatch.setitem(scope,'LiveSmoke',Capture)
    monkeypatch.setitem(scope,'verify_smoke',verify)
    monkeypatch.setattr(scope['runpy'],'run_path',load)
    monkeypatch.setattr(scope['shutil'],'disk_usage',lambda p:NS(free=200*1024**3))
    out=tmp_path/'capture'
    args=NS(output=out,phase='auto',check=False,smoke=None)
    if replay_failure:
        with pytest.raises(ValueError,match='replay mismatch'): await script['run'](args)
        assert events==['capture:smoke','audit:smoke','replay:smoke']
        assert (out/'failure.json').exists() and not (out/'independent').exists()
    else:
        await script['run'](args)
        assert events==['capture:smoke','audit:smoke','replay:smoke','capture:independent','audit:independent']
        assert json.loads((out/'completed.json').read_text())['productionReady'] is False
    assert (out/'protocol.json').exists()
