"""Read-only attribution of previously executed PR58 ledgers and quote paths."""
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from .offline_comparison import scenario_key, write_csv

NAMES = dict(A='single_legacy', B='parallel_legacy', C='parallel_quote_tape')
HORIZONS = (5, 15, 30, 60, 120)


def scalar_subset(d):
    return {k: v for k, v in d.items() if not isinstance(v, (dict, list))}


def path_stats(db, symbol, side, now, sequence, *, entry=None, stop=None, target=None, first_take=None, horizon=60):
    """Executable top quote bounds, never simulated fills. Fail closed on gaps."""
    initial = db.execute('SELECT mono,seq,bid,ask FROM quotes WHERE symbol=? AND mono<=? AND seq<=? ORDER BY mono DESC,seq DESC LIMIT 1', (symbol, now, sequence)).fetchone()
    if not initial or now-initial[0]>1.5 or initial[2] is None or initial[3] is None:
        return dict(status='initial_quote_unavailable')
    sign = 1 if side == 'long' else -1
    entry = entry or initial[3 if sign == 1 else 2]
    rows = db.execute('SELECT mono,seq,bid,ask FROM quotes WHERE symbol=? AND (mono>? OR (mono=? AND seq>?)) AND mono<=? ORDER BY mono,seq', (symbol, now, now, sequence, now+horizon)).fetchall()
    values = []; previous = initial[0]; hits = {}; gap = None
    for mono, seq, bid, ask in [initial, *rows]:
        if bid is None or ask is None or mono-previous>1.5:
            gap = 'quote_gap'; break
        previous = mono
        quote = bid if sign == 1 else ask
        move = sign*(quote-entry)/entry*10000
        values.append((max(0, mono-now), move))
        for label, price, direction in (('stop',stop,-1),('target',target,1),('first_take',first_take,1)):
            if price is not None and direction*sign*(quote-price)>=0 and label not in hits:
                hits[label] = max(0,mono-now)
    status = gap or ('covered' if rows and now+horizon-previous<=1.5 else 'right_censored')
    result = dict(status=status, entry=entry, quote_count=len(values), observed_until_s=max(0,previous-now), **{k+'_first_s':v for k,v in hits.items()})
    if values:
        peak=max(values,key=lambda v:v[1]); trough=min(values,key=lambda v:v[1])
        result.update(observed_mfe_bps=peak[1], observed_mae_bps=trough[1], mfe_first_s=peak[0], mae_first_s=trough[0])
    if status == 'covered': result['markout_bps']=values[-1][1]
    result['first_barrier'] = min(((v,k) for k,v in hits.items() if k!='first_take'),default=(None,None))[1]
    result['scope']='top_quote_bound; no depth/fill guarantee; censored prefix extrema are not full-horizon extrema'
    return result


def reconcile(ledgers):
    indexed={v:{scenario_key(t['symbol'],t['strategyDetails']['scenario']):t for t in ts} for v,ts in ledgers.items()}
    assert all(len(indexed[v])==len(ledgers[v]) for v in indexed), 'multiple fills per identity require explicit matching'
    rows=[]
    for key in sorted(set(indexed['A'])|set(indexed['B'])):
        a,b=(indexed[v].get(key) for v in 'AB')
        delta=(b['netPnl'] if b else 0)-(a['netPnl'] if a else 0)
        # Exact algebra: size contribution at old per-unit return, then per-unit outcome change.
        size=(b['originalQuantity']-a['originalQuantity'])*a['netPnl']/a['originalQuantity'] if a and b else 0
        rows.append(dict(identity=json.dumps(key),effect='retained' if a and b else 'new' if b else 'disappeared',
            single_net=a['netPnl'] if a else 0,parallel_net=b['netPnl'] if b else 0,delta_usdt=delta,
            quantity_contribution_usdt=size,per_unit_path_contribution_usdt=delta-size if a and b else 0,
            unmatched_contribution_usdt=delta if not (a and b) else 0,
            same_entry_time=bool(a and b and a['openedAt']==b['openedAt']),
            mapping='exact symbol/owner/object/episode/side; unmatched episode may share economic excursion',
            interpretation='size/path is algebra, not independent causal portfolio interventions'))
    expected=sum(t['netPnl'] for t in ledgers['B'])-sum(t['netPnl'] for t in ledgers['A'])
    residual=expected-sum(r['delta_usdt'] for r in rows)
    assert abs(residual)<1e-8
    return rows,dict(expected_delta=expected,arithmetic_residual=residual,effects={e:sum(r['delta_usdt'] for r in rows if r['effect']==e) for e in ('new','disappeared','retained')},economic_mapping_uncertainty='unmatched identities not assumed independent economic opportunities')


def evidence_events(path, keys):
    result=defaultdict(lambda:dict(ready=None,opened=None,closed=None,management=[]))
    for line_number,line in enumerate(Path(path).open(encoding='utf-8'),1):
        e=json.loads(line); p=e['payload']; typ=e['event']; s=None
        if typ=='scenario_transition': s=p
        elif typ=='decision': s=p.get('details',{}).get('scenario')
        elif typ=='trade_opened': s=p['plan']['strategy_details'].get('scenario')
        elif typ=='trade_closed': s=p['strategyDetails'].get('scenario')
        if s and s.get('owner'):
            key=scenario_key(e['symbol'],s)
            if key not in keys: continue
            evidence=dict(line=line_number,sequence=e['sequence'],mono_ns=e['mono_ns'],wall=e['wall'],event=typ)
            rec=result[key]
            if s.get('times',{}).get('firstSignal') is not None and rec['ready'] is None: rec['ready']=evidence
            if typ=='trade_opened': rec['opened']=evidence|dict(market=p.get('market'),reasons=p.get('reasons'))
            if typ=='trade_closed': rec['closed']=evidence|dict(market=p.get('market'))
        if typ in ('partial_take','stop_updated','position_stop_updated','breakeven_activated'):
            for key in keys:
                if e['symbol']==key[0] and p.get('side')==key[4]:
                    result[key]['management'].append(dict(line=line_number,sequence=e['sequence'],mono_ns=e['mono_ns'],payload=scalar_subset(p)))
    return dict(result)


def run(root, output):
    root,output=Path(root),Path(output);output.mkdir(parents=True,exist_ok=False)
    ledgers={v:json.loads((root/v/'portfolio_comparison.json').read_text())['variants'][v]['ledger'] for v in NAMES}
    delta,summary=reconcile(ledgers);write_csv(output/'routing_delta_reconciliation.csv',delta)
    route=list(csv.DictReader(Path('docs/pr58-readiness/routing_policy_comparison.csv').open(encoding='utf-8')))
    route_by_key={tuple(r[k] for k in ('symbol','owner','object','episode','side')):r for r in route}
    db=sqlite3.connect(f'file:{(root/"market-index/quotes.sqlite").as_posix()}?mode=ro',uri=True)
    attribution=[];paths=[];evidences={}
    for v,ts in ledgers.items():
        keys={scenario_key(t['symbol'],t['strategyDetails']['scenario']) for t in ts}
        events=evidence_events(root/v/(v+'.jsonl'),keys)
        for i,t in enumerate(ts,1):
            d=t['entryLegs'][0]['plan']['strategy_details'];s=d['scenario'];eco=d['economics'];key=scenario_key(t['symbol'],s)
            ref=f'{NAMES[v]}-{i:02d}';ev=dict(events[key]);tim=s['times'];econ=scalar_subset(eco);sgn=1 if t['side']=='long' else -1
            ev['management']=[m for m in ev['management'] if ev['opened']['mono_ns']<=m['mono_ns']<=ev['closed']['mono_ns'] and m['payload'].get('setupId')==t['setupId']]
            q=t['originalQuantity'];entry=t['entry'];fee=eco['stopExitFeeRate']
            # Actual entry fee plus hypothetical full taker close fee, with no duplicate spread/slip debit.
            breakeven=(sgn*entry+t['entryFeeUsd']/q)/(sgn-fee)
            for origin,start in (('ready',dict(mono_ns=int(tim['firstSignal']*1e9),sequence=int(route_by_key[key][v+'_ready_sequence']))),('fill',ev['opened'])):
                for horizon in HORIZONS:
                    p=path_stats(db,t['symbol'],t['side'],start['mono_ns']/1e9,start['sequence'],entry=entry if origin=='fill' else None,stop=t['initialStop'],target=t['target'],first_take=eco.get('firstTakePrice'),horizon=horizon)
                    paths.append(dict(execution=ref,origin=origin,horizon_s=horizon,**p))
            market_wait=(tim['firstSignal']-tim['prepared'])*1000 if tim.get('prepared') else None
            row=dict(execution=ref,variant=NAMES[v],identity=json.dumps(key),routing_effect=route_by_key[key]['AB_entry_effect'],symbol=t['symbol'],side=t['side'],strategy=t['strategy'],
                object=key[2],episode=key[3],ready_sequence=route_by_key[key][v+'_ready_sequence'],fill_sequence=ev['opened']['sequence'],exit_sequence=ev['closed']['sequence'],
                ready_mono_ns=int(tim['firstSignal']*1e9),fill_mono_ns=ev['opened']['mono_ns'],exit_mono_ns=ev['closed']['mono_ns'],source=str(root/v/(v+'.jsonl')),source_open_line=ev['opened']['line'],source_close_line=ev['closed']['line'],
                regime=d.get('decisionContext',{}).get('localRegime'),flow_class=d.get('flowAlignment',{}).get('classification'),liquidity_state=d.get('liquidityEvidence',{}).get('state'),
                trigger=d.get('causalTriggerSource'),attack_absorbed=d.get('attackAbsorbed'),flow_reversed=d.get('flowReversed'),micro_response_bps=d.get('microPriceResponseBps'),post_absorption_tape_bps=d.get('postAbsorptionTapeResponseBps'),tape_aligned=d.get('tapeResponseAligned'),
                signed_flow5=sgn*d.get('flow',{}).get('imbalance5s',0),signed_move5_bps=sgn*d.get('flow',{}).get('priceMove5sPct',0)*10000,
                market_wait_ms=market_wait,ready_to_admission_ms=(tim['admitted']-tim['firstSignal'])*1000,admission_to_send_ms=(tim['orderSent']-tim['admitted'])*1000,send_to_fill_logical_ms=(ev['opened']['mono_ns']/1e9-tim['orderSent'])*1000,
                historical_parse_to_strategy_ms=d.get('latencyTrace',{}).get('durationsMs',{}).get('parseToStrategy'),timing_scope='logical scheduler intervals; historical receipt trace separate; wall/exchange drift not processing lag',
                entry=entry,initial_stop=t['initialStop'],final_stop=t['stop'],target=t['target'],first_take=eco.get('firstTakePrice'),target_source=d.get('targetSource'),target_impulse_bps=d.get('expectedImpulsePct',0)*10000,
                target_bps=sgn*(t['target']-entry)/entry*10000,stop_bps=sgn*(entry-t['initialStop'])/entry*10000,
                quantity=q,notional=t['originalNotional'],gross=t['grossPnl'],fees=t['fees'],funding=t['fundingPnlUsd'],net=t['netPnl'],fee_bps=t['fees']/t['originalNotional']*10000,
                net_breakeven_taker_fill=breakeven,breakeven_move_bps=sgn*(breakeven-entry)/entry*10000,
                actual_mfe_bps=t['maxFavorableMoveBps'],actual_mae_bps=t['maxAdverseMoveBps'],duration_s=t['closedAt']-t['openedAt'],partial_planned=eco['partialPlanned'],partial_taken=t['partialTaken'],partial_net_at_trigger=eco['partialNetAtTriggerUsd'],plan_net_at_target=eco['netAtTargetUsd'],plan_loss_at_stop=eco['netAtStopUsd'],exit_reason=t['reason'],
                risk_scale=d.get('riskScale'),execution_modes=json.dumps(eco['executionProfile']),embedded_entry_slippage_usdt=eco['embeddedEntrySlippageUsd'],actual_exit_quality=t['executionQuality']['quality'],
                main_cause='pending_evidence_review',secondary_factors='',mapping='exact identity; see reconciliation uncertainty')
            assert abs(row['gross']-row['fees']+row['funding']-row['net'])<1e-8
            attribution.append(row)
            evidences[ref]=dict(events=ev,scenario={k:x for k,x in s.items() if k!='preparation'},decision={k:d.get(k) for k in ('decisionContext','flow','formingCandle','entryFreshness','opportunityFreshness','stateTiming','latencyTrace','entryContextAssessment','qualityFactors','levelLifecycle')},economics=econ,plan={k:x for k,x in t['entryLegs'][0]['plan'].items() if k!='strategy_details'})
    write_csv(output/'trade_failure_attribution.csv',attribution);write_csv(output/'trade_quote_paths.csv',paths)
    (output/'trade-evidence.json').write_text(json.dumps(evidences,indent=2)+'\n')
    (output/'routing-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    # All ready controls, including favorable outcomes, retaining the already published gap/cost scope.
    controls=[]
    for v in NAMES:
        for r in route:
            if not r.get(v+'_ready_ns'):continue
            controls.append(dict(variant=NAMES[v],symbol=r['symbol'],owner=r['owner'],side=r['side'],effect=r['AB_ready_effect'],identity=json.dumps([r[k] for k in ('symbol','owner','object','episode','side')]),coverage=r.get(v+'_status'),mfe60_bps=r.get(v+'_mfe_bps'),gross60_bps=r.get(v+'_gross_markout_bps'),illustrative_net60_bps=r.get(v+'_diagnostic_net_bps'),filled=r.get(v+'_closed_trades')))
    write_csv(output/'all_ready_controls.csv',controls)
    print(json.dumps(summary))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('output');a=p.parse_args();run(a.root,a.output)
