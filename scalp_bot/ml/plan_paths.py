"""Raw-book path audit of existing labels; no relabelling, features or fitting."""
import bisect
from collections import defaultdict,Counter
import json
from pathlib import Path
from ..bybit import OrderBookState,OrderBookSequenceError,MarketMessage
from .dataset import read_events
from .plan_diagnosis import row_key


def audit(source,dataset,capture,output):
    target=[]
    for line in Path(dataset).open():
        r=json.loads(line)
        if r['ref']['capture_id']==capture and r['split'] in ('train','calibration','validation'):target.append(r)
    last=max(r['label_end_ns'] for r in target)+1_500_000_000
    by_symbol=defaultdict(list)
    for r in target:by_symbol[r['ref']['symbol']].append(r)
    books={};quotes=defaultdict(list);counts=Counter()
    for e in read_events(source):
        if e['processingMonoNs']>last:break
        s=e['symbol'];b=e['body'];now=e['processingMonoNs'];seq=e['sequence']
        if s not in by_symbol:continue
        if e['kind']=='bootstrap':books[s]=OrderBookState(50)
        if e['kind']=='transport' and b['phase'] in ('fault','connecting','cancelled') and f'orderbook.50.{s}' in b['topics']:
            books[s]._clear();quotes[s].append((now,seq,None,None))
        if e['kind']=='market_message' and b['topic'].startswith('orderbook.50.'):
            try:
                book=books[s].apply(MarketMessage(**b));quotes[s].append((now,seq,book.best_bid,book.best_ask))
            except OrderBookSequenceError:
                books[s]._clear();quotes[s].append((now,seq,None,None));counts['sequence_errors']+=1
            counts['quotes']+=1
    result=[]
    for s,rs in by_symbol.items():
        tape=quotes[s];times=[t[0] for t in tape]
        for r in rs:
            a=bisect.bisect_left(times,r['entry_ns']); b=bisect.bisect_right(times,r['exit_ns']);end=bisect.bisect_right(times,r['label_end_ns'])
            sign=1 if r['side']=='long' else -1
            item=dict(key=row_key(r),status='covered',mfe_bps=None,mae_bps=None,n=0,original_exit_ns=r['exit_ns'],original_label=r['label'])
            if a==len(tape) or times[a]!=r['entry_ns'] or b==0 or times[b-1]!=r['exit_ns']:
                item['status']='entry_or_exit_event_not_found'
            else:
                values=[];previous=r['entry_ns'];first_stop=first_target=None
                for now,seq,bid,ask in tape[a:b]:
                    if bid is None or ask is None or now-previous>1_500_000_000:item['status']='gap';break
                    previous=now;price=bid if sign==1 else ask;values.append(sign*(price-r['entry'])/r['entry']*10000)
                    if sign*(price-r['stop'])<=0 and first_stop is None:first_stop=now
                    if sign*(price-r['target'])>=0 and first_target is None:first_target=now
                if values:item.update(best_signed_exit_bps=max(values),mfe_bps=max(0,max(values)),mae_bps=min(0,min(values)),n=len(values),stop_first_ns=first_stop,target_first_ns=first_target)
                expected=first_stop if r['label']=='stop_first' else first_target if r['label']=='target_first' else r['exit_ns']
                item['barrier_exit_matches']=expected==r['exit_ns']
                if not item['barrier_exit_matches']:item['status']='barrier_mismatch'
                later=[sign*((q[2] if sign==1 else q[3])-r['entry'])/r['entry']*10000 for q in tape[a:end] if q[2] is not None and q[3] is not None]
                # Explicitly separate after-stop diagnostic bounds, never count them as successes.
                full=tape[a:end];covered=bool(full) and r['label_end_ns']-full[-1][0]<=1_500_000_000 and all(x[2] is not None and x[3] is not None and (i==0 or x[0]-full[i-1][0]<=1_500_000_000) for i,x in enumerate(full))
                item['full_horizon_covered']=covered
                item['after_original_exit_full_horizon_mfe_bps']=max(later) if later and covered else None
            result.append(item)
    output=Path(output)
    with output.open('x',encoding='utf-8') as f:
        for r in result:f.write(json.dumps(r,separators=(',',':'))+'\n')
    report=dict(source=str(source),capture=capture,rows=len(result),counts=dict(counts),status=dict(Counter(r['status'] for r in result)),scope='existing entry/stop/target/exit; executable top quote extrema, not new fills or portfolio')
    output.with_suffix('.manifest.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('dataset');p.add_argument('capture');p.add_argument('output');a=p.parse_args();audit(a.source,a.dataset,a.capture,a.output)
