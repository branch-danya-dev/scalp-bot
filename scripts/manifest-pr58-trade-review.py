"""Hash this task's artifacts after all measured runs have finished."""
import argparse,hashlib,json,subprocess
from pathlib import Path

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def build(root,output):
    root,output=Path(root),Path(output)
    files=[dict(path=str(p.resolve()),bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(root.rglob('*')) if p.is_file() and p.resolve()!=output.resolve()]
    inputs=[]
    for p in [*[Path(f'G:/scalp-bot/data/pr58-completion/{v}/{v}.jsonl') for v in 'ABC'],Path('docs/pr58-readiness/run-provenance.json'),Path('docs/pr58-readiness/dataset-manifest-v2.json'),Path('docs/pr58-readiness/model-manifest-v2.json'),Path('models/ml/impulse-v1-final/manifest.json')]:
        inputs.append(dict(path=str(p.resolve()),bytes=p.stat().st_size,sha256=sha(p)))
    code=[]
    for name in ('scalp_bot/engine.py','scalp_bot/offline_benchmark.py','scalp_bot/offline_benchmark_trace.py','scalp_bot/offline_trade_diagnosis.py','scalp_bot/ml/plan_diagnosis.py','scalp_bot/ml/plan_paths.py','scripts/audit-pr58-causal-controls.py','scripts/audit-pr58-sampling.py','scripts/benchmark-isolated.py','scripts/benchmark-gc-probe.py','scripts/summarize-pr58-performance.py','scripts/build-pr58-trade-review.py','scripts/manifest-pr58-trade-review.py'):
        p=Path(name);code.append(dict(path=name,sha256=sha(p)))
    result=dict(schema=1,artifact_root=str(root),retention='retain until explicitly removed by owner; large local raw files are not committed',
        measurement_build='6cc28d8',ledger_build='1d7d9ab',working_head_at_manifest=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        files=files,inputs=inputs,code=code,source_scope='complete historical load hashes in performance-isolated.json; original capture/index provenance referenced, not replaced by a new certification',
        final_ci='final-ci.json and its checksum receipt are written after publishing and are necessarily outside this pre-publication file snapshot')
    with output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(dict(files=len(files),bytes=sum(x['bytes'] for x in files),inputs=len(inputs))))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('output');a=p.parse_args();build(a.root,a.output)
