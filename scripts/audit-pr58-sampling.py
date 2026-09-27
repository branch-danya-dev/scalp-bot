"""Read-only causal sampling audit; fixed all-ready predicate, no future labels."""
import argparse,bisect,json
from collections import defaultdict,Counter
from pathlib import Path
from scalp_bot.offline_comparison import scenario_key

def audit(root,output):
    root=Path(root);samples=defaultdict(list);ends={}
    for line in (root/'dataset-v2-verified/dataset.jsonl').open():
        r=json.loads(line)
        if r['split'] not in ('train','calibration','validation'):continue
        ref=r['ref'];capture=ref['capture_id'];at=ref['available_mono_ns']
        samples[capture,ref['symbol'],r['side']].append((at,ref['source_sequence']))
        ends[capture]=max(ends.get(capture,0),at)
    for rows in samples.values():rows.sort()
    ready=[]
    for directory,capture in [('morning-context','capture-20260926T093515Z'),('noon-context','capture-20260926T121926Z')]:
        seen=set()
        for line in (root/directory/'B.jsonl').open():
            e=json.loads(line)
            if e['event']!='scenario_transition':continue
            s=e['payload'];at=s.get('times',{}).get('firstSignal')
            if at is None:continue
            at=round(at*1e9);key=scenario_key(e['symbol'],s)
            if key in seen or at>ends[capture]:continue
            seen.add(key);rows=samples[capture,e['symbol'],s['side']]
            i=bisect.bisect_right(rows,(at,e['sequence']))-1;prior=rows[i] if i>=0 else None
            ready.append(dict(capture=capture,symbol=e['symbol'],owner=s['owner'],side=s['side'],ready_ns=at,source_sequence=e['sequence'],prior_sample_age_ms=(at-prior[0])/1e6 if prior else None,same_sample_sequence=bool(prior and prior[1]==e['sequence'])))
    result=dict(scope='all causal ready events in development; no outcome selection',ready=ready,counts=dict(Counter('within_1s' if r['prior_sample_age_ms'] is not None and r['prior_sample_age_ms']<=1000 else 'older_or_missing' for r in ready)))
    with Path(output).open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(dict(ready=len(ready),counts=result['counts'])))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('output');a=p.parse_args();audit(a.root,a.output)
