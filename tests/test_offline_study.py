import gzip,json
import pytest
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.offline_index import index
from scalp_bot.offline_study import advance
from scalp_bot.runtime_clock import ReplayRuntimeClock
from types import SimpleNamespace


def journal():
    result=[];previous=None
    for i,kind in enumerate(('manifest','clock_read','run_end'),1):
        r=dict(schema='replay-input-v4',sequence=i,previousHash=previous,kind=kind,symbol=None,
            processingWallSeconds=100+i,processingMonoNs=i*10**9,body={})
        r['hash']=fingerprint(r);previous=r['hash']
        result.append(dict(event='replay_input',symbol=None,payload=r))
    return result


def test_truncated_archive_preserves_only_verified_rows_and_never_invents_footer(tmp_path):
    source=tmp_path/'raw.gz';data=b''.join(json.dumps(r).encode()+b'\n' for r in journal())
    original=gzip.compress(data)[:-8];source.write_bytes(original)
    report=index(source,tmp_path/'derived')
    assert report['compressed_tail_truncated'] and not report['gzip_crc_complete']
    assert report['complete_run_end'] and not report['footer_present']
    assert report['selected_row_content_hashes_recomputed']
    assert source.read_bytes()==original


def test_corrupt_semantic_row_is_rejected_even_with_valid_links(tmp_path):
    rows=journal();rows[0]['payload']['body']['future']=123
    source=tmp_path/'raw.gz';source.write_bytes(gzip.compress(b''.join(json.dumps(r).encode()+b'\n' for r in rows)))
    with pytest.raises(ValueError,match='content hash'):index(source,tmp_path/'derived')
    assert not (tmp_path/'derived'/'index-manifest.json').exists()


async def test_logical_scheduler_does_not_inspect_next_market_observation():
    clock=ReplayRuntimeClock(wall_seconds=100,mono_ns=10_000_000_000);seen=[]
    async def evaluate(symbol,reason,capture_id):seen.append((clock.perf_counter_ns(),symbol))
    e=SimpleNamespace(clock=clock,config=SimpleNamespace(arbiter_interval_seconds=.25),
        scheduled=[(10_100_000_000,1,'AAA','quote',None)],running=False,_run_event_evaluation=evaluate)
    arbiter=await advance(e,10_100_000_000,100.1,10_250_000_000)
    assert not seen  # Input at the same timestamp is available first by declared tie policy.
    await advance(e,10_200_000_000,100.2,arbiter)
    assert seen==[(10_100_000_000,'AAA')]


def test_quote_index_keeps_each_raw_fast_update_and_transport_unknowns(tmp_path,monkeypatch):
    from scalp_bot import offline_market_index as module
    import sqlite3
    rows=[]
    def event(kind,body):
        n=len(rows)+1;rows.append(dict(kind=kind,symbol='AAA',sequence=n,body=body,processingMonoNs=n*10**9,processingWallSeconds=100+n))
    event('bootstrap',{})
    for u in (1,2,3):
        event('market_message',dict(topic='orderbook.50.AAA',type='snapshot' if u==1 else 'delta',ts=u*1000,data={'u':u,'seq':u,'b':[['100',str(u)]],'a':[['101','2']]}))
    event('transport',dict(phase='fault',topics=['orderbook.50.AAA']))
    event('run_end',{})
    monkeypatch.setattr(module,'source_events',lambda source:iter(rows))
    monkeypatch.setattr(module,'episodes',lambda path:({},{}))
    report=module.build('source','runs',tmp_path/'index')
    assert report['counts']['quotes']==3
    with sqlite3.connect(tmp_path/'index'/'quotes.sqlite') as db:
        actual=db.execute('SELECT seq,bid,ask FROM quotes ORDER BY seq').fetchall()
    assert actual==[(2,100.,101.),(3,100.,101.),(4,100.,101.),(5,None,None)]
