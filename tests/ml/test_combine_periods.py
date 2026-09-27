import json
from pathlib import Path
import pytest
from scalp_bot.ml.combine_periods import combine
from scalp_bot.ml.features import FEATURE_NAMES,FEATURE_SCHEMA
from scalp_bot.ml.history.importer import sha256_file


def source(path,capture,market_base):
    path.mkdir()
    rows=[]
    for t in range(10,601,10):
        for symbol in ('AAA','BBB'):
            rows.append(dict(ref=dict(capture_id=capture,symbol=symbol,available_mono_ns=t*10**9,
                market_time_ms=market_base+t*1000),features=[None]*len(FEATURE_NAMES),
                label=('target_first','stop_first','timeout')[t//10%3],label_end_ns=(t+30)*10**9,
                episode=f'{capture}:{t//60}',split='old'))
    (path/'dataset.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (path/'manifest.json').write_text(json.dumps(dict(feature_schema=FEATURE_SCHEMA,policy_sha256='fixed',
        dataset_sha256=sha256_file(path/'dataset.jsonl'),inventory={s:dict(instrument=dict(symbol=s,tick_size=.01 if capture=='morning' else .02)) for s in ('AAA','BBB')})))


def test_global_purge_and_dated_inventories_are_preserved(tmp_path):
    morning,noon=tmp_path/'morning',tmp_path/'noon'
    source(morning,'morning',1_000_000);source(noon,'noon',10_000_000)
    plan=tmp_path/'plan.json';plan.write_text(json.dumps(dict(noon_boundaries_ns=[240*10**9,420*10**9])))
    out=tmp_path/'combined';m=combine(morning,noon,plan,out)
    rows=[json.loads(s) for s in (out/'dataset.jsonl').read_text().splitlines()]
    assert all(r['split']=='train' for r in rows if r['ref']['capture_id']=='morning')
    noonrows=[r for r in rows if r['ref']['capture_id']=='noon']
    for r in noonrows:
        t=r['ref']['available_mono_ns']//10**9
        expected='purged' if 180<=t<300 or 360<=t<480 else 'calibration' if t<240 else 'validation' if t<420 else 'test'
        assert r['split']==expected
    assert m['inventory_by_capture']['morning']['AAA']['instrument']['tick_size']==.01
    assert m['inventory_by_capture']['noon']['AAA']['instrument']['tick_size']==.02
    assert not m['untouched_external_test'] and not m['external_2024_admitted']
    assert not m['training_ready']  # validation intentionally smaller than admission floor
    assert sha256_file(out/'dataset.jsonl')==m['dataset_sha256']


def test_changed_source_rejected_before_output(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b';source(a,'morning',1_000_000);source(b,'noon',10_000_000)
    (a/'dataset.jsonl').write_text((a/'dataset.jsonl').read_text()+'{}\n')
    plan=tmp_path/'plan';plan.write_text(json.dumps(dict(noon_boundaries_ns=[240*10**9,420*10**9])))
    with pytest.raises(ValueError,match='source dataset changed'):combine(a,b,plan,tmp_path/'out')
    assert not (tmp_path/'out').exists()
