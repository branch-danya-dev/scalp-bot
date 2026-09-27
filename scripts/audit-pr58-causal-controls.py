"""Recover exact ready/entry snapshots and receipt witnesses from existing events."""
import argparse,csv,json,sqlite3,zlib
from collections import defaultdict
from pathlib import Path
from scalp_bot.offline_comparison import scenario_key
from scalp_bot.manifest_validation import fingerprint

def build(trades,source,output):
    trades,source,output=map(Path,(trades,source,output));output.mkdir(parents=True,exist_ok=False)
    rows=list(csv.DictReader((trades/'trade_failure_attribution.csv').open()))
    targets=defaultdict(list)
    for r in rows:targets[r['source']].append(r)
    snapshots={};table=[]
    for path,rs in targets.items():
        wanted={(int(r['ready_sequence']),tuple(json.loads(r['identity']))):r for r in rs}
        for line_no,line in enumerate(Path(path).open(),1):
            e=json.loads(line)
            if e['event']!='decision':continue
            p=e['payload'];d=p.get('details',{});s=d.get('scenario')
            if not s or not s.get('owner'):continue
            key=(e['sequence'],scenario_key(e['symbol'],s));r=wanted.get(key)
            if r is None or r['execution'] in snapshots:continue
            snapshots[r['execution']]=dict(source=path,line=line_no,event=e)
        for r in rs:
            snap=snapshots.get(r['execution'])
            d=snap['event']['payload']['details'] if snap else {}
            table.append(dict(execution=r['execution'],ready_status='exact_source_sequence_and_episode' if snap else 'unavailable',
                ready_source_line=snap['line'] if snap else None,ready_source_sequence=r['ready_sequence'],
                ready_action=snap['event']['payload']['action'] if snap else None,
                ready_regime=d.get('decisionContext',{}).get('localRegime'),fill_regime=r['regime'],
                ready_flow_class=d.get('flowAlignment',{}).get('classification'),fill_flow_class=r['flow_class'],
                ready_micro_response_bps=d.get('microPriceResponseBps'),fill_micro_response_bps=r['micro_response_bps'],
                ready_tape_aligned=d.get('tapeResponseAligned'),fill_tape_aligned=r['tape_aligned'],
                evidence='exact recorded decision, no nearest research quote or rebuilt future context'))
    evidence=json.loads((trades/'trade-evidence.json').read_text());witnesses=[]
    db=sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True)
    for execution,ev in evidence.items():
        events=ev['events'];seen=set()
        for m in events['management']:
            seq=m['sequence']
            if seq in seen:continue
            seen.add(seq);blob=db.execute('SELECT payload FROM inputs WHERE idx=?',(seq,)).fetchone()
            assert blob is not None
            raw=json.loads(zlib.decompress(blob[0]));assert raw['sequence']==seq
            assert raw['hash']==fingerprint({k:v for k,v in raw.items() if k!='hash'})
            body=raw['body']
            if not (body.get('topic') or '').startswith('publicTrade.'):continue
            opened=events['opened']['mono_ns'];received=body['receipt_mono_ns']
            witnesses.append(dict(execution=execution,sequence=seq,source_hash=raw['hash'],source=str(source),
                opened_mono_ns=opened,receipt_mono_ns=received,processing_mono_ns=raw['processingMonoNs'],
                received_before_open=received<opened,receipt_to_open_ms=(opened-received)/1e6,
                topic=body['topic'],trade_count=len(body['data']),management=m))
    db.close()
    (output/'ready-context-snapshots.json').write_text(json.dumps(snapshots,indent=2)+'\n')
    (output/'receipt-witnesses.json').write_text(json.dumps(witnesses,indent=2)+'\n')
    with (output/'ready-vs-fill.csv').open('x',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(table[0]));w.writeheader();w.writerows(table)
    print(json.dumps(dict(executions=len(rows),ready_snapshots=len(snapshots),trade_receipt_witnesses=len(witnesses),pre_entry=sum(r['received_before_open'] for r in witnesses))))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('trades');p.add_argument('source');p.add_argument('output');a=p.parse_args();build(a.trades,a.source,a.output)
