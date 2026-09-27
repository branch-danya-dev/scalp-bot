"""Bybit application heartbeat is distinct from WebSocket control ping."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from scalp_bot.demo_paper import transport

class FakePrivateSocket:
    def __init__(self,stop,*,pong=True,traffic=False):
        self.stop=stop;self.now=0.;self.last_application=0.;self.sent=[];self.queue=[]
        self.pong=pong;self.traffic=traffic;self.controls={}
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def send(self,raw):
        row=json.loads(raw);self.sent.append(row)
        if row['op'] in ('auth','subscribe'):self.queue.append(json.dumps({'op':row['op'],'success':True}))
        if row['op']=='ping':
            self.last_application=self.now
            if self.pong:self.queue.append(json.dumps({'op':'pong','req_id':row['req_id'] if self.pong is True else 'stale-ping','args':['1']}))
    async def recv(self):
        if self.queue:return self.queue.pop(0)
        self.now+=1
        # A control-frame pong does not reset the application's idle timer.
        if self.now-self.last_application>600:raise ConnectionError('private idle cutoff')
        if self.now>=660:self.stop.set()
        if self.traffic:return json.dumps({'topic':'position','data':[]})
        raise asyncio.TimeoutError

def setup(monkeypatch,stop,**kwargs):
    ws=FakePrivateSocket(stop,**kwargs)
    def connect(url,**controls):
        assert url==transport.PRIVATE_WS
        ws.controls=controls
        return ws
    monkeypatch.setattr(transport,'NoRedirectConnect',connect)
    monkeypatch.setattr(transport,'time',SimpleNamespace(time=lambda:1000.,monotonic=lambda:ws.now))
    return ws

async def test_idle_private_connection_survives_documented_ten_minute_cutoff(monkeypatch):
    stop=asyncio.Event();ws=setup(monkeypatch,stop);gaps=[]
    async def gap(reason):
        gaps.append(reason)
        if reason!='reconnected':stop.set()
    async def message(row):pass
    await transport.private_stream(transport.Credentials('FAKE_KEY','FAKE_SECRET','123'),message,gap,stop)
    assert gaps==['reconnected']
    assert ws.now>=660 and sum(r['op']=='ping' for r in ws.sent)>=32
    assert ws.controls['ping_interval']==15 and ws.controls['ping_timeout']==10


@pytest.mark.parametrize("traffic",[False,True])
@pytest.mark.parametrize("pong",[False,"wrong_id"])
async def test_missing_application_pong_still_halts_even_with_data(monkeypatch,traffic,pong):
    stop=asyncio.Event();ws=setup(monkeypatch,stop,pong=pong,traffic=traffic);events=[];gaps=[]
    async def gap(reason):
        gaps.append(reason)
        if reason!='reconnected':stop.set()
    async def message(row):pass
    await transport.private_stream(transport.Credentials('FAKE_KEY','FAKE_SECRET','123'),message,gap,stop,
        diagnostics=lambda e,p:events.append((e,p)))
    assert gaps==['reconnected','private_ws_gap'] and ws.now==30
    failure=events[-1][1]
    assert failure['stage']=='application_pong_timeout'
    assert failure['ping_count']==1 and failure['pong_count']==0
    assert 'FAKE_KEY' not in json.dumps(events) and 'FAKE_SECRET' not in json.dumps(events)

async def test_heartbeat_does_not_replace_private_facts_or_freshness(monkeypatch):
    stop=asyncio.Event();ws=setup(monkeypatch,stop,traffic=True);events=[];facts=[]
    async def gap(reason):assert reason=='reconnected'
    async def message(row):facts.append(row)
    await transport.private_stream(transport.Credentials('KEY','SECRET','123'),message,gap,stop,
        diagnostics=lambda e,p:events.append((e,p)))
    assert len(facts)==660 and all(r['topic']=='position' for r in facts)
    assert len(events)>=32 and all(e=='private_heartbeat' for e,p in events)
    assert all(p['ping']==p['pong'] for e,p in events)

async def test_close_diagnostics_record_code_but_never_remote_reason(monkeypatch):
    stop=asyncio.Event();ws=setup(monkeypatch,stop);events=[]
    class Closed(Exception):
        rcvd=SimpleNamespace(code=1001);sent=SimpleNamespace(code=1001)
    receive=ws.recv
    async def recv():
        if ws.queue:return await receive()
        raise Closed('FAKE_SECRET_REMOTE_REASON')
    ws.recv=recv
    async def gap(reason):
        if reason!='reconnected':stop.set()
    async def message(row):pass
    await transport.private_stream(transport.Credentials('KEY','SECRET','123'),message,gap,stop,
        diagnostics=lambda e,p:events.append((e,p)))
    assert events[-1][1]['received_close_code']==1001
    assert events[-1][1]['stage']=='receive'
    assert 'FAKE_SECRET_REMOTE_REASON' not in json.dumps(events)
