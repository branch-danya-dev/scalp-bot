"""Combine predeclared dated captures; reject spec conflicts and overlapping splits."""
from collections import Counter
from math import isfinite
import json
from pathlib import Path
from .dataset import temporal_split
from .features import FEATURE_NAMES
from .history.importer import sha256_file
from .learning import load_rows


def combine(morning,noon,plan,output):
    morning,noon,output=Path(morning),Path(noon),Path(output)
    frozen=json.loads(Path(plan).read_text())
    manifests=[json.loads((p/'manifest.json').read_text()) for p in (morning,noon)]
    assert manifests[0]['feature_schema']==manifests[1]['feature_schema']
    assert manifests[0]['policy_sha256']==manifests[1]['policy_sha256']
    rows=[];inventories={}
    for i,p in enumerate((morning,noon)):
        manifest=manifests[i]
        if sha256_file(p/'dataset.jsonl')!=manifest['dataset_sha256']:raise ValueError('source dataset changed')
        for symbol,item in manifest['inventory'].items():
            for field in ('tick_size','qty_step','min_order_qty','min_notional_value'):
                value=item['instrument'].get(field)
                if not isinstance(value,(float,int)) or not isfinite(value) or value<=0:
                    raise ValueError(f'unverified mandatory specification: {symbol} {field}')
        subset=load_rows(p/'dataset.jsonl')
        for r in subset:
            inventories[r['ref']['capture_id']]=manifest['inventory']
            r['split']='train' if i==0 else None
        if i:
            bounds=frozen['noon_boundaries_ns']
            for r in subset:
                now=r['ref']['available_mono_ns'];end=r['label_end_ns']
                left=now//60_000_000_000*60_000_000_000;right=max(end,left+60_000_000_000)
                r['split']='purged' if any(left<=b<=right for b in bounds) else ('calibration','validation','test')[sum(now>b for b in bounds)]
        rows.extend(subset)
    # Separate capture domains are ordered by exchange observation time, not by
    # assuming unrelated monotonic clocks are interchangeable.
    assert max(r['ref']['market_time_ms']+30000 for r in rows if r['split']=='train')<min(r['ref']['market_time_ms'] for r in rows if r['split']=='calibration')
    output.mkdir(parents=True,exist_ok=False)
    with (output/'dataset.jsonl').open('x') as f:
        for r in rows:f.write(json.dumps(r,separators=(',',':'))+'\n')
    m=dict(manifests[0]);m.update(schema_version=2,candidate_version='impulse-v2',
        dataset_sha256=sha256_file(output/'dataset.jsonl'),source_datasets=[dict(path=str(p.resolve()),sha256=v['dataset_sha256']) for p,v in zip((morning,noon),manifests)],
        frozen_protocol=frozen,inventory_by_capture=inventories,
        sampling_universe={capture:sorted(inventory) for capture,inventory in inventories.items()},
        universe_note='all eligible activated symbols in the predeclared additional captures; legacy plan_policy universe text describes v1 only; numeric payoff policy unchanged',
        evidence_scope='different archived same-day periods; held-out labels frozen before training; market dates previously manually reviewed',
        untouched_external_test=False,external_2024_admitted=False,
        labels=dict(Counter(r['label'] for r in rows)),splits=dict(Counter(r['split'] for r in rows)),
        coverage={name:sum(r['features'][i] is not None for r in rows)/len(rows) for i,name in enumerate(FEATURE_NAMES)},
        mask_mean={name:sum(r['features'][i] or 0 for r in rows)/len(rows) for i,name in enumerate(FEATURE_NAMES) if name.endswith('_known')},
        split_classes={s:dict(Counter(r['label'] for r in rows if r['split']==s)) for s in ('train','calibration','validation','test')},
        split_period_market_ms={s:[min(r['ref']['market_time_ms'] for r in rows if r['split']==s),max(r['ref']['market_time_ms'] for r in rows if r['split']==s)] for s in ('train','calibration','validation','test')})
    m.pop('split_boundaries_ns',None);m.pop('capture_id',None)
    m['training_ready']=all(m['splits'].get(s,0)>=30 for s in ('train','calibration','validation','test')) and len(m['split_classes']['train'])==3
    (output/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
    print(json.dumps({k:m[k] for k in ('labels','splits','split_classes','mask_mean','training_ready')}))
    return m


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    for name in ('morning','noon','plan','output'):p.add_argument(name)
    a=p.parse_args();combine(a.morning,a.noon,a.plan,a.output)
