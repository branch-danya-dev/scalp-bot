import asyncio
import gc
import pytest
from scalp_bot import pipeline_evidence as diagnostic
from scalp_bot.bybit import MarketMessage, _DiagnosticMarketQueue, _process_market_queue


@pytest.mark.asyncio
async def test_optional_diagnostics_preserve_fifo_payload_and_queue_cleanup(monkeypatch):
    rows=[];monkeypatch.setattr(diagnostic,'market_sink',rows.append)
    queue=_DiagnosticMarketQueue(maxsize=2);stop=asyncio.Event();seen=[]
    for i in range(2):queue.put_nowait(MarketMessage(topic='publicTrade.A',event_id=str(i),data=[i]))
    async def callback(message):
        seen.append(message.data)
        if len(seen)==2:stop.set()
    await _process_market_queue(queue,callback,stop,max_lag_seconds=0)
    await asyncio.wait_for(queue.join(),.1)
    assert seen==[[0],[1]] and [r['identity'] for r in rows]==['0','1']
    assert all(r['enqueue_ns']<=r['callback_start_ns']<=r['callback_end_ns'] for r in rows)


def test_gc_probe_preserves_gc_and_records_generation_overlap():
    rows=[];enabled=gc.isenabled()
    with diagnostic.AllocationProbe(rows.append) as probe:
        with probe.callback('fixture'):
            cycle=[];cycle.append(cycle);del cycle
            gc.collect(0)
    assert gc.isenabled()==enabled
    assert any(r['kind']=='gc' and r['generation']==0 and r['overlaps_callback'] for r in rows)
    assert any(r['kind']=='allocation_pressure' for r in rows)
