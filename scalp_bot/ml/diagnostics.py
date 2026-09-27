"""Explain abstention, including pre/post calibration and missing-value effects."""
import argparse
from collections import Counter
import json
from pathlib import Path
import numpy as np
from sklearn.metrics import roc_auc_score,average_precision_score
from .learning import Predictor, CLASSES, load_rows, raw_matrix, transform, temperature
from .features import FEATURE_NAMES


def describe(values):
    return dict(zip(('min','p50','p90','p99','max'),np.quantile(values,[0,.5,.9,.99,1]).tolist()))


def diagnose(dataset, model, output):
    rows=load_rows(Path(dataset)/'dataset.jsonl');predictor=Predictor(model)
    m=predictor.metadata
    assert list(predictor.model.classes_)==list(range(len(CLASSES)))
    assert tuple(m['classes'])==CLASSES
    result=dict(class_order=CLASSES,catboost_classes=predictor.model.classes_.tolist(),
        feature_order_matches=m['feature_order']==list(FEATURE_NAMES)+['plan_side'],
        temperature=m['temperatures']['catboost'],preprocessing_fit=m['preprocessing']['fit_split'],
        threshold=.55,groups={},freshness='offline probability selection has no freshness gate; shadow freshness is separate',
        label_warning='fixed 30s plan; independent overlapping labels, not a portfolio')
    for split in ('train','calibration','validation','test'):
        subset=[r for r in rows if r['split']==split]
        raw=raw_matrix(subset);matrix=transform(raw,m['preprocessing'])
        assert np.isfinite(matrix).all()
        before=predictor.model.predict_proba(matrix,thread_count=1)
        after=temperature(before,m['temperatures']['catboost'])
        assert np.allclose(after,predictor.predict_rows(subset))
        groups={'all':np.ones(len(subset),dtype=bool)}
        for key in ('side','symbol'):
            values=[r['side'] if key=='side' else r['ref']['symbol'] for r in subset]
            for value in sorted(set(values)):groups[key+':'+value]=np.asarray([v==value for v in values])
        for key,mask in groups.items():
            selected=[r for r,keep in zip(subset,mask) if keep]
            result['groups'][split+'/'+key]=dict(n=len(selected),labels=dict(Counter(r['label'] for r in selected)),
                before={c:describe(before[mask,i]) for i,c in enumerate(CLASSES)},
                after={c:describe(after[mask,i]) for i,c in enumerate(CLASSES)},
                selected_before=int((before[mask,0]>=.55).sum()),selected_after=int((after[mask,0]>=.55).sum()),
                abstention_reason_counts={'target_probability_below_0.55':int((after[mask,0]<.55).sum())},
                target_rate=sum(r['label']=='target_first' for r in selected)/len(selected),
                mean_net_usdt=float(np.mean([r['net_usdt'] for r in selected])))
        target=np.asarray([r['label']=='target_first' for r in subset])
        result.setdefault('ranking',{})[split]=dict(roc_auc=float(roc_auc_score(target,after[:,0])) if len(set(target))==2 else None,average_precision=float(average_precision_score(target,after[:,0])) if target.any() else None)
        if split!='test':
            bins=[]
            edges=(0,.01,.025,.05,.1,.2,.4,.55,1.0000001)
            for left,right in zip(edges,edges[1:]):
                selected=[r for r,p in zip(subset,after[:,0]) if left<=p<right]
                bins.append(dict(left=left,right=min(right,1),n=len(selected),episodes=len({r['episode'] for r in selected}),labels=dict(Counter(r['label'] for r in selected)),mean_net_usdt=float(np.mean([r['net_usdt'] for r in selected])) if selected else None))
            result.setdefault('development_score_bins',{})[split]=bins
        result.setdefault('missing_fraction',{})[split]={n:float(np.isnan(raw[:,i]).mean()) for i,n in enumerate(m['feature_order'])}
    result['conclusion']='class/preprocessing/order parity checked; selection is target-first probability, not maximum class confidence; inspect rare target rate and temperature distribution'
    Path(output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:{x:v[x] for x in ('n','labels','selected_before','selected_after','target_rate')} for k,v in result['groups'].items() if k.endswith('/all')}))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('dataset');p.add_argument('model');p.add_argument('output');a=p.parse_args()
    diagnose(a.dataset,a.model,a.output)
