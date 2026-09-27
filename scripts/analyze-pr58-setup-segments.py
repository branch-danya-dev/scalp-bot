import csv
import argparse
import json
from collections import defaultdict
from pathlib import Path
from scalp_bot.offline_comparison import scenario_key
from scalp_bot.setup_segments import setup_segment, segment_key

parser = argparse.ArgumentParser()
parser.add_argument('data_root')
parser.add_argument('--output', required=True)
args = parser.parse_args()
root = Path(args.data_root)
controls = list(csv.DictReader((root/'audit-pr58-trade-plan/trades-v3-entry-snapshot/all_ready_controls.csv').open()))
variants = dict(A='single_legacy', B='parallel_legacy', C='parallel_quote_tape')
output = {}
for variant, name in variants.items():
    targets = {tuple(json.loads(r['identity'])): r for r in controls if r['variant'] == name}
    found = {}
    path = root/f'pr58-completion/{variant}/{variant}.jsonl'
    for line in path.open(encoding='utf-8'):
        event = json.loads(line)
        if event['event'] != 'decision':
            continue
        p = event['payload']; d = p.get('details') or {}; s = d.get('scenario') or {}
        if p.get('action') not in ('long','short') or not s.get('owner'):
            continue
        key = scenario_key(event['symbol'],s)
        if key in targets and key not in found:
            found[key] = setup_segment(p['strategy'], p['action'], d, entry=p.get('entry'), stop=p.get('stop'))
    groups = defaultdict(list)
    for key, control in targets.items():
        segment = found.get(key) or {'strategy': control['owner']}
        groups[segment_key(segment)].append(control)
    result = []
    for key, rows in sorted(groups.items()):
        covered = [r for r in rows if r['coverage']=='covered_top_of_book']
        nets = [float(r['illustrative_net60_bps']) for r in covered if r['illustrative_net60_bps']]
        mfes = [float(r['mfe60_bps']) for r in covered if r['mfe60_bps']]
        result.append(dict(dimensions=key, count=len(rows), covered=len(covered),
            positiveIllustrative60s=sum(v>0 for v in nets), meanIllustrativeNet60Bps=sum(nets)/len(nets) if nets else None,
            meanQuoteMfe60Bps=sum(mfes)/len(mfes) if mfes else None,
            portfolioPnl=None, expectancyR=None, maeR=None,
            missing='ready controls do not establish depth/maker fills, MAE, complete lifecycle or net R'))
    output[name] = dict(count=len(targets), matchedContexts=len(found), segments=result)
Path(args.output).write_text(json.dumps(dict(
    scope='Previously studied PR58 top-quote controls; overlapping labels never portfolio PnL', variants=output),indent=2)+'\n')
print({k:(v['count'],v['matchedContexts']) for k,v in output.items()})
