"""Read-only archived-source diagnostic; never rewrites raw manifests/tapes."""
import argparse, asyncio, gzip, hashlib, json, sys, traceback, zipfile
from pathlib import Path

def main():
    p=argparse.ArgumentParser()
    p.add_argument('archive'); p.add_argument('primary'); p.add_argument('output')
    a=p.parse_args(); out=Path(a.output); out.mkdir(parents=True, exist_ok=False)
    root=out/'source'; root.mkdir()
    with zipfile.ZipFile(a.archive) as archive:
        for name in archive.namelist():
            target=(root/name).resolve()
            if not target.is_relative_to(root.resolve()): raise ValueError('unsafe archive path')
        archive.extractall(root)
    sys.path.insert(0,str(root))
    from scalp_bot.capture_replay import IndexedInputs
    from scalp_bot.offline_bootstrap import restore_cold_engine
    from scalp_bot.offline_scheduler import OfflineScheduledReplay
    from scalp_bot import offline_segment
    from scalp_bot.manifest_validation import fingerprint
    from scalp_bot.run_manifest import code_provenance,runtime_provenance
    with gzip.open(a.primary,'rb') as f:
        prefix=[json.loads(next(f))['payload'] for _ in range(3)]
    manifest=prefix[1]['body']['manifest']
    receipt=dict(scope='archived native scheduler replay diagnostic; no performance or natural-path acceptance',
        primarySha256=hashlib.file_digest(open(a.primary,'rb'),'sha256').hexdigest(),
        archiveSha256=hashlib.file_digest(open(a.archive,'rb'),'sha256').hexdigest(),
        source=code_provenance(root), runtimeSha256=fingerprint(runtime_provenance()),
        expectedSourceSha256=manifest['code']['sourceSha256'], expectedRuntimeSha256=manifest['runtimeSha256'])
    (out/'binding.json').write_text(json.dumps(receipt,indent=2))
    engine=restore_cold_engine(prefix) # Unchanged archive code, mandatory exact binding.
    print('Exact archived source/runtime admitted; indexing ALL native rows',flush=True)
    rows=IndexedInputs.build(Path(a.primary),out/'native.sqlite')
    receipt['rows']=len(rows)
    # Observe the original failure before scope cleanup masks it; no consumption changes.
    original_append=offline_segment._Cursor.append
    original_read=offline_segment._Cursor.read
    first=[]
    def remember(cursor,action,exc):
        if not first:
            first.append(dict(action=action,index=cursor.index,expected=cursor.peek(),
                exception=type(exc).__name__,message=str(exc),stack=traceback.format_exc()))
    def append(cursor,kind,symbol,body):
        try: return original_append(cursor,kind,symbol,body)
        except Exception as exc:
            remember(cursor,dict(kind=kind,symbol=symbol,body=body),exc); raise
    def read(cursor,method):
        try: return original_read(cursor,method)
        except Exception as exc:
            remember(cursor,dict(clock=method),exc); raise
    offline_segment._Cursor.append=append
    offline_segment._Cursor.read=read
    class Sink:
        def __init__(self): self.count=0; self.digest=hashlib.sha256()
        def append(self,row):
            self.count+=1; self.digest.update(fingerprint(row).encode())
    sink=Sink()
    try:
        result=asyncio.run(OfflineScheduledReplay(engine).apply(rows[3:],event_sink=sink))
        receipt.update(status='REPLAY_COMPLETED_WITHOUT_OUTPUT_COMPARISON',result={k:v for k,v in result.items() if k!='events'})
    except Exception as exc:
        receipt.update(status='NOT_MET',failure=str(exc),exception=type(exc).__name__,firstDivergence=first,
            stack=traceback.format_exc())
    finally:
        rows.connection.close()
    receipt.update(outputPrefixCount=sink.count,outputPrefixSha256=sink.digest.hexdigest(),
        nativeLabelGate='INCONCLUSIVE',controlledGate='NOT_MET')
    (out/'replay.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps({k:receipt.get(k) for k in ('status','rows','failure','outputPrefixCount')}),flush=True)

if __name__=='__main__': main()
