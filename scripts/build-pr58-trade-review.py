"""Assemble fixed PR58 diagnosis tables; annotations are evidence review, not rules."""
import argparse,csv,hashlib,json,shutil
from collections import defaultdict,Counter
from pathlib import Path

CAUSES={
 1:('positive_control','Aligned breakout; genuine post-entry partial pays, total net positive.'),
 2:('unconfirmed_direction_reversal','Reclaim/absorption within bearish trend and opposed flow; no durable executable rebound.'),
 3:('unconfirmed_direction_reversal','Bearish impulse; 6.289bps stop narrower than round-trip fees; partial disabled as unprofitable.'),
 4:('unreliable_data','ETH gap prevents full-path conclusion; observed favorable move below taker break-even; target beyond frozen movement scale.'),
 5:('unconfirmed_direction_reversal','Bullish impulse/opposed flow; brief short response fails. Nearby single/parallel objects differ, not independent opportunities.'),
 6:('insufficient_scale_relative_to_costs','Paid partial exists; runner stop above current executable quote causes immediate taker close; complete fees exceed gross.'),
 7:('confirmed_technical_defect','Raw21025769 received22.575003ms before position, retrospectively fills maker partial. Old ledger is not post-fix PnL.'),
 8:('unconfirmed_direction_reversal','Bullish impulse/mixed flow; 375bps distant target versus42bps frozen impulse; no favorable book excursion before loss.')}

def read(path):return list(csv.DictReader(Path(path).open(encoding='utf8')))
def write(path,rows):
 fields=list(dict.fromkeys(k for r in rows for k in r))
 with Path(path).open('x',newline='',encoding='utf8') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def build(trades,ml,output):
 trades,ml,output=map(Path,(trades,ml,output));output.mkdir(parents=True,exist_ok=True)
 rows=read(trades/'trade_failure_attribution.csv');assert len(rows)==21
 identities=defaultdict(list)
 for r in rows:
  signature=tuple(r[k] for k in ('symbol','side','strategy','entry','initial_stop','target','quantity','net','fill_mono_ns','exit_mono_ns'))
  uid=hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:12];r['unique_execution_id']=uid;identities[uid].append(r['execution'])
  number=int(r['execution'].rsplit('-',1)[1]);cause,text=CAUSES[number]
  r['main_cause']=cause;r['secondary_factors']=text;r['annotation_evidence']='ATTRIBUTION_NOTES.md + local trade-evidence.json + raw source sequence'
 for r in rows:r['same_execution_in_variants']=' | '.join(identities[r['unique_execution_id']])
 write(output/'trade_failure_attribution.csv',rows)
 for name in ('routing_delta_reconciliation.csv','trade_quote_paths.csv','all_ready_controls.csv'):
  assert not (output/name).exists();shutil.copyfile(trades/name,output/name)
 for name in ('ml_plan_diagnostics.csv','ml_score_payoff.csv'):
  assert not (output/name).exists();shutil.copyfile(ml/name,output/name)
 groups=defaultdict(list)
 for r in read(trades/'all_ready_controls.csv'):
  for dim in ('owner','side','effect','coverage'):groups[(r['variant'],dim,r[dim])].append(r)
 controls=[]
 for (variant,dim,value),rs in groups.items():
  covered=[r for r in rs if r['coverage']=='covered_top_of_book']
  nets=[float(r['illustrative_net60_bps']) for r in covered if r['illustrative_net60_bps']]
  controls.append(dict(variant=variant,dimension=dim,group=value,ready=len(rs),covered=len(covered),positive_illustrative_60s=sum(n>0 for n in nets),nonpositive_illustrative_60s=sum(n<=0 for n in nets),mean_illustrative_60s_bps=sum(nets)/len(nets) if nets else None,scope='predeclared complete ready groups; existing13bps top quote diagnostic, not individual ledger or portfolio'))
 write(output/'ready_control_summary.csv',controls)
 original=Path('docs/run-reviews/two-hour-20260926/execution_controls.json');controls=json.loads(original.read_text())
 write(output/'original_broker_controls.csv',[dict(control=r['id'],symbol=r['symbol'],original_open_line=r['open_line'],original_close_line=r['close_line'],net=(r.get('close') or {}).get('netPnl'),source=str(original),source_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),scope='unchanged original broker controls, distinct from current counterfactual executions') for r in controls])
 summary=json.loads((trades/'routing-summary.json').read_text());summary.update(ledger_observations=len(rows),unique_execution_paths=len(identities),unique_episode_keys=len({r['identity'] for r in rows}),causes=dict(Counter(r['main_cause'] for r in rows if r['variant']=='parallel_legacy')),ledger_base_commit='1d7d9ab',post_fix_portfolio_result=None)
 (output/'trade-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
 (output/'diagnostic-manifest.json').write_text((ml/'manifest.json').read_text())
 print(json.dumps(summary))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('trades');p.add_argument('ml');p.add_argument('output');a=p.parse_args();build(a.trades,a.ml,a.output)
