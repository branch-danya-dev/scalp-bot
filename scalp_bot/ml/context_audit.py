"""Report actual information coverage, source domains and causal label fences."""
from collections import Counter,defaultdict
import json,math
from pathlib import Path
from .features import FEATURE_NAMES
from .learning import load_rows
from .history.importer import sha256_file


def audit(dataset,output):
    dataset=Path(dataset);m=json.loads((dataset/'manifest.json').read_text())
    if sha256_file(dataset/'dataset.jsonl')!=m['dataset_sha256']:raise ValueError('dataset checksum')
    rows=load_rows(dataset/'dataset.jsonl');groups=defaultdict(list);episodes=defaultdict(set)
    for r in rows:
        ref=r['ref'];start=ref['available_mono_ns']
        if len(r['features'])!=len(FEATURE_NAMES) or any(x is not None and not math.isfinite(x) for x in r['features']):raise ValueError('feature schema/numeric')
        if not start+100_000_000<=r['entry_ns']<=r['exit_ns']<=r['label_end_ns']+1_500_000_000:raise ValueError('label causality')
        if abs(r['gross_usdt']-r['fees_usdt']-r['net_usdt'])>1e-10:raise ValueError('fee accounting')
        if r['split']!='purged':episodes[(ref['capture_id'],r['episode'])].add(r['split'])
        groups[(ref['capture_id'],ref['symbol'],r['split'])].append(r)
    if any(len(s)>1 for s in episodes.values()):raise ValueError('episode split leakage')
    coverage=[]
    masks=[n for n in FEATURE_NAMES if n.endswith('_known') or '_covered_' in n]
    for (capture,symbol,split),group in sorted(groups.items()):
        coverage.append(dict(capture=capture,symbol=symbol,split=split,rows=len(group),
            buckets=len({r['episode'] for r in group}),epochs=len({r['ref']['selection_epoch'] for r in group}),
            label_counts=dict(Counter(r['label'] for r in group)),
            actual_known_fraction={n:sum(r['features'][FEATURE_NAMES.index(n)] or 0 for r in group)/len(group) for n in masks},
            nonmissing_fraction={n:sum(r['features'][i] is not None for r in group)/len(group) for i,n in enumerate(FEATURE_NAMES)},
            example={k:group[0][k] for k in ('ref','features','entry_ns','exit_ns','label_end_ns','quantity','fees_usdt')}))
    report=dict(dataset_sha256=m['dataset_sha256'],rows=len(rows),episodes=len(episodes),causal_fences_passed=True,
        interpretation='known masks measured by value; nonmissing mask fields alone do not prove information; 60s buckets are dependence groups, not independent market events',
        units='same FEATURE_SCHEMA and actual MarketContext; linear base quantity and USDT quote from dated bootstrap',groups=coverage)
    Path(output).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='groups'}));return report


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('dataset');p.add_argument('output');a=p.parse_args();audit(a.dataset,a.output)
