import asyncio
import pytest
from scalp_bot.bybit import _stream_topics


@pytest.mark.parametrize('failure',['handshake','subscribe','receive'])
async def test_transport_error_phase_timing_and_redaction(monkeypatch,failure):
    stop=asyncio.Event();events=[]
    class Socket:
        async def __aenter__(self):
            if failure=='handshake':raise TimeoutError('secret URL user:password?token=secret')
            return self
        async def __aexit__(self,*args):return False
        async def send(self,*args,**kwargs):
            if failure=='subscribe':raise ConnectionError('secret payload')
        async def recv(self,*args,**kwargs):raise ConnectionError('secret payload')
    monkeypatch.setattr('scalp_bot.bybit.websockets.connect',lambda *a,**k:Socket())
    def notify(event):
        events.append(event)
        if event['phase']=='fault':stop.set()
    async def callback(message):pass
    await _stream_topics('wss://user:password@example.invalid/path?token=secret',['orderbook.50.AAA'],callback,stop,on_transport=notify)
    fault=next(e for e in events if e['phase']=='fault')
    expected={'handshake':'connect_handshake','subscribe':'subscribe_send','receive':'receive_or_process'}[failure]
    assert fault['diagnostics']['stage']==expected
    assert fault['diagnostics']['host']=='example.invalid'
    assert fault['diagnostics']['stageElapsedMs']>=0
    assert not any(secret in str(events) for secret in ('password','token','payload'))
    assert events[-1]['phase']=='drained'
