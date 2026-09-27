"""Compare all reconstructed episodes; quote index is raw L50, never research_frame."""
from collections import Counter
import csv,json,sqlite3,zlib
from pathlib import Path
from .domain import OrderBook,Side,TradeTick
from .instrument import InstrumentSpec
from .strategy.rejection_response import assess_reclaim_response


def scenario_key(symbol,s):
    return (symbol,s['owner'],(s.get('objectRef') or {}).get('key',''),s.get('episodeKey',''),s['side'])


def episodes(path):
    result={};reclaims=[];manifest=None
    with Path(path).open() as stream:
        for line in stream:
            event=json.loads(line);p=event['payload'];s=None
            if event['event']=='bot_started':manifest=p['manifest']
            if event['event']=='scenario_transition':s=p
            elif event['event']=='decision':s=p.get('details',{}).get('scenario')
            elif event['event']=='risk_reject':s=(p.get('decision') or {}).get('details',{}).get('scenario')
            elif event['event']=='first_reclaim':s=p.get('scenario');reclaims.append(event)
            if not s or not s.get('owner'):continue
            key=scenario_key(event['symbol'],s)
            row=result.setdefault(key,dict(symbol=key[0],owner=key[1],object=key[2],episode=key[3],side=key[4],
                first_ns=event['mono_ns'],last_ns=event['mono_ns'],scenario_ids=set(),states=set(),risk_reasons=set(),
                ready_ns=None,ready_sequence=None,ready_market_ms=None,ready_wall=None,admitted=False,filled=False,reclaim=None))
            row['scenario_ids'].add(s['scenarioId']);row['states'].add(s['state']);row['last_ns']=event['mono_ns']
            times=s.get('times',{})
            if times.get('firstSignal') is not None and row['ready_ns'] is None:
                row.update(ready_ns=int(times['firstSignal']*1e9),ready_sequence=event['sequence'],ready_wall=event['wall'],
                    ready_market_ms=(p.get('marketContext') or {}).get('observedAtMs'),plan=s)
            if row['ready_ns'] and row['ready_market_ms'] is None and abs(event['mono_ns']-row['ready_ns'])<1000:
                row['ready_market_ms']=(p.get('marketContext') or {}).get('observedAtMs')
            row['admitted'] |= times.get('admitted') is not None
            row['filled'] |= times.get('fill') is not None
            if s.get('lastRejection'):row['risk_reasons'].add(s['lastRejection']['reason'])
            if event['event']=='risk_reject':row['risk_reasons'].add(p['reason'])
            if event['event']=='first_reclaim' and (row['reclaim'] is None or event['mono_ns']<row['reclaim']['mono_ns']):row['reclaim']=event
    return result,manifest


def markout(db,symbol,side,now,horizon=60):
    initial=db.execute('SELECT mono,seq,bid,ask FROM quotes WHERE symbol=? AND mono<=? ORDER BY mono DESC LIMIT 1',(symbol,now)).fetchone()
    if not initial or now-initial[0]>1.5:return dict(status='initial_quote_unavailable')
    entry=initial[3] if side=='long' else initial[2];sign=1 if side=='long' else -1
    rows=db.execute('SELECT mono,bid,ask FROM quotes WHERE symbol=? AND mono>? AND mono<=? ORDER BY mono',(symbol,now,now+horizon)).fetchall()
    if not rows or now+horizon-rows[-1][0]>1.5:return dict(status='right_censored')
    previous=initial[0]
    values=[]
    for mono,bid,ask in rows:
        if mono-previous>1.5:return dict(status='quote_gap')
        previous=mono;quote=bid if side=='long' else ask
        values.append(sign*(quote-entry)/entry*10000)
    return dict(status='covered_top_of_book',entry=entry,entry_sequence=initial[1],horizon_seconds=horizon,
        gross_markout_bps=values[-1],mfe_bps=max(values),mae_bps=min(values),
        diagnostic_net_bps=values[-1]-13,fee_slippage_bps=13,
        cost_scope='0.055% taker each side plus 1bps each fill; top-of-book diagnostic, not actual portfolio execution')


def write_csv(path,rows):
    names=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w',newline='',encoding='utf-8') as stream:
        w=csv.DictWriter(stream,fieldnames=names);w.writeheader();w.writerows(rows)


def compare(root,quotes,baseline,output,inputs=None):
    root,output=Path(root),Path(output);output.mkdir(parents=True,exist_ok=True)
    all_rows={};manifests={};portfolios={}
    for variant in 'ABC':
        all_rows[variant],manifests[variant]=episodes(root/variant/(variant+'.jsonl'))
        portfolios[variant]=json.loads((root/variant/'portfolio_comparison.json').read_text())
        assert portfolios[variant]['complete']
    base_config=manifests['A']['config']
    for variant in 'BC':
        differences={k:(base_config[k],v) for k,v in manifests[variant]['config'].items() if base_config[k]!=v}
        expected={} if variant=='B' else {'research_rejection_response_policy':('legacy','quote_tape_v1')}
        assert differences==expected,differences
    db=sqlite3.connect(f'file:{Path(quotes).resolve().as_posix()}?mode=ro',uri=True)
    raw=sqlite3.connect(f'file:{Path(inputs).resolve().as_posix()}?mode=ro',uri=True) if inputs else None
    keys=sorted(set().union(*(set(v) for v in all_rows.values())))
    rows=[];effects={}
    for key in keys:
        row=dict(zip(('symbol','owner','object','episode','side'),key))
        for variant in 'ABC':
            r=all_rows[variant].get(key)
            row[variant+'_observed']=r is not None
            if not r:continue
            row.update({variant+'_'+k:r[k] for k in ('ready_ns','ready_sequence','admitted','filled')})
            row[variant+'_risk_reasons']=' | '.join(sorted(r['risk_reasons']))
            reclaim=r['reclaim']
            row[variant+'_reclaim_sequence']=reclaim['sequence'] if reclaim else None
            row[variant+'_reclaim_ms']=reclaim['payload']['state']['reclaim_at_ms'] if reclaim else None
            row[variant+'_reclaim_quote']=reclaim['payload']['state']['reclaim_quote'] if reclaim else None
            if r['ready_ns']:
                observation=markout(db,key[0],key[4],r['ready_ns']/1e9)
                row.update({variant+'_'+k:v for k,v in observation.items() if k!='cost_scope'})
                row[variant+'_reclaim_to_ready_ms']=(r['ready_ns']-reclaim['mono_ns'])/1e6 if reclaim else None
                if raw and reclaim and r['ready_market_ms']:
                    trades=[]
                    query="SELECT payload FROM inputs WHERE symbol=? AND wall>=? AND wall<=? AND idx<=? AND kind='market_message' ORDER BY idx"
                    for (payload,) in raw.execute(query,(key[0],reclaim['wall']-1,r['ready_wall'],r['ready_sequence'])):
                        message=json.loads(zlib.decompress(payload))['body']
                        if message['topic'].startswith('publicTrade.'):
                            trades.extend(TradeTick(int(t['T']),float(t['p']),float(t['v']),t['S']) for t in message['data'])
                    state=reclaim['payload']['state']
                    quote=db.execute('SELECT bid,ask FROM quotes WHERE symbol=? AND mono<=? ORDER BY mono DESC LIMIT 1',(key[0],r['ready_ns']/1e9)).fetchone()
                    response=assess_reclaim_response(key[4],state['reclaim_at_ms'],state['reclaim_quote'],
                        quote[1] if key[4]=='long' else quote[0],trades,r['ready_market_ms'],
                        manifests[variant]['config']['weak_level_rejection_micro_response_min_bps'])
                    row.update({variant+'_'+k:v for k,v in response.items() if k in ('quoteResponseBps','tapeResponseBps','tradeCount','allowed')})
        for left,right in (('A','B'),('B','C')):
            a,b=row.get(left+'_ready_ns'),row.get(right+'_ready_ns')
            row[left+right+'_ready_effect']='retained' if a and b else 'new' if b else 'disappeared' if a else 'neither'
            row[left+right+'_delay_ms']=(b-a)/1e6 if a and b else None
        rows.append(row)
    write_csv(output/'routing_policy_comparison.csv',rows)
    write_csv(output/'rejection_episode_comparison.csv',[r for r in rows if r['owner']=='weak_level_rejection'])
    # Preserve all 201 original scenarios (including all 81 rejection scenarios),
    # even when a counterfactual does not produce an exact episode identity.
    baseline_rows=list(csv.DictReader(Path(baseline).open(encoding='utf-8-sig')))
    coverage=[]
    for original in baseline_rows:
        row={k:original[k] for k in ('scenario_id','symbol','owner','side','object','initial_episode','last_episode','prepared','ready','admitted','filled')}
        for variant in 'ABC':
            candidates=[r for key,r in all_rows[variant].items() if key[0]==original['symbol'] and key[1]==original['owner'] and key[2]==original['object'] and key[4]==original['side']]
            exact=[r for r in candidates if r['episode'] in (original['initial_episode'],original['last_episode'])]
            row[variant+'_exact_episode_matches']=len(exact)
            row[variant+'_same_object_candidates']=len(candidates)
            row[variant+'_status']='exact_episode' if exact else 'object_only_not_proven_same_excursion' if candidates else 'not_reconstructed'
            row[variant+'_ready_exact']=sum(r['ready_ns'] is not None for r in exact)
        coverage.append(row)
    write_csv(output/'baseline_scenario_coverage.csv',coverage)
    for left,right in (('A','B'),('B','C')):
        label=left+right
        counts=Counter(r[label+'_ready_effect'] for r in rows)
        diagnostic_lost=[r for r in rows if r[label+'_ready_effect']=='disappeared' and r.get(left+'_diagnostic_net_bps',-1)>0]
        effects[label]=dict(exact_identity_ready_effects=dict(counts),disappeared_positive_60s_diagnostics=len(diagnostic_lost),
            interpretation='identity-level diagnostic; different quote-clock excursion ids are not automatically different economic opportunities')
    portfolio=dict(schema_version=2,complete=True,scope='independent actual PaperBroker/RiskEngine paths conditional on captured membership and availability',
        original_scheduler_parity=False,original_capture_unchanged=True,common_config_verified=True,
        source_last_sequence={v:portfolios[v]['last_sequence'] for v in 'ABC'},variants={},effects=effects,
        markout_scope='top-of-book independent side markouts, 13bps illustrative costs, NOT portfolio execution',
        original_scenarios=len(coverage),original_rejection_scenarios=sum(r['owner']=='weak_level_rejection' for r in coverage),
        original_ready_scenarios=sum(r['ready']=='True' for r in coverage))
    for v in 'ABC':
        r=portfolios[v]['variants'][v];ledger=r['ledger']
        exposed=[g for g in portfolios[v]['gaps'] if g.get('position_open') and any(t.startswith('orderbook.50.') for t in g.get('topics',[]))]
        portfolio['variants'][v]=dict(balance=r['balance'],conditional_realized_net=sum(t['netPnl'] for t in ledger),
            trades=len(ledger),wins=sum(t['netPnl']>0 for t in ledger),ledger=ledger,
            open_positions=r['open_positions'],reclaims=r['reclaims'],fast_gap_position_events=exposed,
            full_path_certified_net=None if exposed or r['open_positions'] else sum(t['netPnl'] for t in ledger),
            unique_episodes=len(all_rows[v]),ready_episodes=sum(x['ready_ns'] is not None for x in all_rows[v].values()))
    for left,right in (('A','B'),('B','C')):
        portfolio['effects'][left+right]['conditional_net_difference']=portfolio['variants'][right]['conditional_realized_net']-portfolio['variants'][left]['conditional_realized_net']
    (output/'portfolio_comparison.json').write_text(json.dumps(portfolio,indent=2)+'\n')
    (output/'run-provenance.json').write_text(json.dumps(manifests,indent=2)+'\n')
    print(json.dumps({v:{k:x for k,x in r.items() if k not in ('ledger','open_positions','fast_gap_position_events')} for v,r in portfolio['variants'].items()}))
    return portfolio


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('quotes');p.add_argument('baseline');p.add_argument('output');p.add_argument('--inputs');a=p.parse_args()
    compare(a.root,a.quotes,a.baseline,a.output,a.inputs)
