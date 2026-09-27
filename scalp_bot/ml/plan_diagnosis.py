"""Frozen-plan economics and validation-only ranking of immutable V2 weights."""
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np
from .features import FEATURE_NAMES
from .history.importer import sha256_file
from .learning import CLASSES, Predictor, raw_matrix, rule_probabilities, temperature, transform


def write_csv(path, rows):
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)


def summaries(rows):
    if not rows:return dict(n=0,time_groups=0,mean_net=None)
    net=np.array([r['net_usdt'] for r in rows]); timeout=[r for r in rows if r['label']=='timeout']
    out=dict(n=len(rows),time_groups=len({r['episode'] for r in rows}),symbols=len({r['ref']['symbol'] for r in rows}),
        mean_net=float(net.mean()),median_net=float(np.median(net)),p05_net=float(np.quantile(net,.05)),p95_net=float(np.quantile(net,.95)),
        mean_gross=float(np.mean([r['gross_usdt'] for r in rows])),mean_fees=float(np.mean([r['fees_usdt'] for r in rows])),positive_fraction=float((net>0).mean()),
        timeout_positive=sum(r['net_usdt']>0 for r in timeout),timeout_negative=sum(r['net_usdt']<0 for r in timeout),timeout_zero=sum(r['net_usdt']==0 for r in timeout),
        largest_symbol_fraction=max(Counter(r['ref']['symbol'] for r in rows).values())/len(rows),largest_time_group_fraction=max(Counter(r['episode'] for r in rows).values())/len(rows))
    out.update({c+'_fraction':sum(r['label']==c for r in rows)/len(rows) for c in CLASSES})
    out['largest_symbol']=Counter(r['ref']['symbol'] for r in rows).most_common(1)[0][0]
    for c in CLASSES:
        payouts=[r['net_usdt'] for r in rows if r['label']==c]
        out[c+'_mean_net']=float(np.mean(payouts)) if payouts else None
    for positive in (True,False):
        payouts=[r['net_usdt'] for r in timeout if (r['net_usdt']>0)==positive]
        out['timeout_'+('positive' if positive else 'nonpositive')+'_mean_net']=float(np.mean(payouts)) if payouts else None
    for name,values in [('duration_s',[(r['exit_ns']-r['entry_ns'])/1e9 for r in rows]),('target_bps',[abs(r['target']/r['entry']-1)*1e4 for r in rows]),('stop_bps',[abs(r['stop']/r['entry']-1)*1e4 for r in rows]),('entry_delay_ms',[(r['entry_ns']-r['ref']['available_mono_ns'])/1e6 for r in rows])]:
        out.update({name+'_'+q:float(np.quantile(values,p)) for q,p in [('p05',.05),('p50',.5),('p95',.95),('max',1)]})
    for field in ('mfe_bps','mae_bps'):
        vals=[r['path'][field] for r in rows if r.get('path',{}).get('status')=='covered']
        out[field+'_covered']=len(vals)
        for name,q in [('p05',.05),('p50',.5),('p95',.95)]:out[field+'_'+name]=float(np.quantile(vals,q)) if vals else None
    return out


def uncertainty(rows):
    groups=defaultdict(list)
    for r in rows:groups[r['episode']].append(r['net_usdt'])
    if len(groups)<2:return dict(group_ci_low=None,group_ci_high=None,leave_one_group_low=None,leave_one_group_high=None)
    sums=np.array([sum(v) for v in groups.values()]);ns=np.array([len(v) for v in groups.values()]);n=len(ns)
    rng=np.random.default_rng(1729);ix=rng.integers(0,n,size=(1000,n));boot=sums[ix].sum(axis=1)/ns[ix].sum(axis=1)
    loo=(sums.sum()-sums)/(ns.sum()-ns)
    return dict(group_ci_low=float(np.quantile(boot,.025)),group_ci_high=float(np.quantile(boot,.975)),leave_one_group_low=float(loo.min()),leave_one_group_high=float(loo.max()))


def state_groups(row):
    f=dict(zip(FEATURE_NAMES,row['features']));sign=1 if row['side']=='long' else -1
    result={'liquidity':'known' if f['liquidity_known']==1 else 'unknown'}
    for name,value,limits in [('signed_move5',f['micro_move_5s_bps'],(0,2)),('signed_flow5',f['trade_imbalance_5s'],(0,.2)),('range',f['range_bps'],(15,30)),('spread',f['spread_bps'],(1,3)),('depth',f['top5_depth_usd'],(1000,10000))]:
        if value is None:result[name]='unknown';continue
        if name.startswith('signed'):value*=sign
        result[name]='low' if (value<=limits[0] if name.startswith('signed') else value<limits[0]) else 'medium' if value<limits[1] else 'high'
    return result


def fixed_rank_slices(scores):
    order=np.argsort(scores,kind='stable');n=len(order)
    yield 'all',np.arange(n)
    for j,ix in enumerate(np.array_split(order,10),1):yield f'decile_{j:02d}',ix
    for percent in (1,5,10):yield f'top_{percent}pct',order[-math.ceil(n*percent/100):]
    yield 'threshold_0.55',np.flatnonzero(scores>=.55)


def run(dataset,model,output,paths=None):
    dataset,model,output=map(Path,(dataset,model,output));output.mkdir(parents=True,exist_ok=False)
    predictor=Predictor(model);meta=predictor.metadata;dm=json.loads((dataset/'manifest.json').read_text())
    assert sha256_file(dataset/'dataset.jsonl')==meta['dataset_sha256']==dm['dataset_sha256']
    assert sha256_file(model/'logistic.npz')==meta['logistic_sha256']
    assert dm['policy_sha256']==meta['policy_sha256']
    # Test payloads are never put into analysis/prediction containers.
    rows=[];excluded=Counter()
    for line in (dataset/'dataset.jsonl').open():
        r=json.loads(line)
        if r['split'] not in ('train','calibration','validation'):excluded[r['split']]+=1;continue
        rows.append(r)
    if paths:
        path_rows={r['key']:r for r in map(json.loads,Path(paths).open())}
        for r in rows:r['path']=path_rows.get(row_key(r),{})
    groups=defaultdict(list)
    for r in rows:
        f=dict(zip(FEATURE_NAMES,r['features'])); r['ratios']={name:value for name,value in ((
            'target_to_range',30/f['range_bps'] if f['range_bps'] and f['range_bps']>0 else None),('stop_to_range',15/f['range_bps'] if f['range_bps'] and f['range_bps']>0 else None),
            ('target_to_spread',30/f['spread_bps'] if f['spread_bps'] and f['spread_bps']>0 else None),('nominal_to_depth',100/f['top5_depth_usd'] if f['top5_depth_usd'] and f['top5_depth_usd']>0 else None),('target_to_abs_move5',30/abs(f['micro_move_5s_bps']) if f['micro_move_5s_bps'] else None))}
        for dimension,value in dict(all='all',symbol=r['ref']['symbol'],side=r['side'],capture=r['ref']['capture_id'],outcome=r['label'],**state_groups(r)).items():
            groups[(r['split'],dimension,value)].append(r)
        sign=1 if r['side']=='long' else -1
        assert abs(sign*(r['exit']-r['entry'])*r['quantity']-r['gross_usdt'])<1e-8
        assert abs((r['entry']+r['exit'])*r['quantity']*.00055-r['fees_usdt'])<1e-8
        assert abs(r['gross_usdt']-r['fees_usdt']-r['net_usdt'])<1e-8
    diagnostics=[]
    for (split,dimension,value),rs in groups.items():
        out=dict(split=split,dimension=dimension,group=value,**summaries(rs))
        for field in rs[0]['ratios']:
            values=[r['ratios'][field] for r in rs if r['ratios'][field] is not None]
            out[field+'_known']=len(values);out[field+'_median']=float(np.median(values)) if values else None
        diagnostics.append(out)
    write_csv(output/'ml_plan_diagnostics.csv',diagnostics)
    validation=[r for r in rows if r['split']=='validation'];train=[r for r in rows if r['split']=='train']
    weights=np.load(model/'logistic.npz',allow_pickle=False);matrix=transform(raw_matrix(validation),meta['preprocessing'])
    logits=matrix@weights['coef'].T+weights['intercept'];logits-=logits.max(axis=1,keepdims=True);lp=np.exp(logits);lp/=lp.sum(axis=1,keepdims=True)
    scores={'catboost':predictor.predict_rows(validation)[:,0],'logistic':temperature(lp,meta['temperatures']['logistic'])[:,0],
        'fixed_rule':rule_probabilities(validation)[:,0],'train_prior':np.full(len(validation),sum(r['label']=='target_first' for r in train)/len(train))}
    ranking=[];raw=[]
    for model_name,values in scores.items():
        for group,ix in fixed_rank_slices(values):
            rs=[validation[int(i)] for i in ix]
            ranking.append(dict(model=model_name,group=group,score_min=float(values[ix].min()) if len(ix) else None,score_max=float(values[ix].max()) if len(ix) else None,
                tied_score_fraction=1-len(set(values[ix]))/len(ix) if len(ix) else None,**summaries(rs),**uncertainty(rs)))
        for r,score in zip(validation,values):raw.append(dict(key=row_key(r),model=model_name,score=float(score),episode=r['episode'],symbol=r['ref']['symbol'],side=r['side'],label=r['label'],net_usdt=r['net_usdt']))
    ranking.append(dict(model='no_trade',group='all',n=0,time_groups=0,mean_net=None,cash_change_usdt=0))
    write_csv(output/'ml_score_payoff.csv',ranking);write_csv(output/'validation_scores.csv',raw)
    (output/'manifest.json').write_text(json.dumps(dict(base_head='1d7d9ab',dataset_path=str(dataset),dataset_sha256=dm['dataset_sha256'],model_path=str(model),model_sha256=meta['model_sha256'],policy_sha256=meta['policy_sha256'],excluded=dict(excluded),protocol='docs/pr58-trade-plan/DIAGNOSTIC_PROTOCOL.md',scope='dependent window payouts, not portfolio PnL; no training or threshold change',rows=len(rows)),indent=2)+'\n')
    print(json.dumps(dict(development_rows=len(rows),validation_rows=len(validation),excluded=dict(excluded))))


def row_key(r):
    return ':'.join(str(x) for x in (r['ref']['capture_id'],r['ref']['symbol'],r['ref']['available_mono_ns'],r['side']))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('dataset');p.add_argument('model');p.add_argument('output');p.add_argument('--paths');a=p.parse_args();run(a.dataset,a.model,a.output,a.paths)
