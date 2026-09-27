"""Build a disposable semantic index from immutable compressed input journals."""
import argparse
from collections import Counter
import gzip,hashlib,json,sqlite3,time,zlib
from pathlib import Path
import msgspec
from .ml.history.importer import sha256_file
from .manifest_validation import fingerprint

KINDS={'manifest','bootstrap','rest_context','scanner_result','clock_sample','clock_error','market_message','transport','control','symbol_lifecycle','run_end'}


def index(source,output):
    source,output=Path(source),Path(output)
    output.mkdir(parents=True,exist_ok=False)
    db=sqlite3.connect(output/'inputs.sqlite')
    db.execute('CREATE TABLE inputs(idx INTEGER PRIMARY KEY,kind TEXT,payload BLOB)')
    previous=None;counts=Counter();selected=Counter();rows=0;first=last=None;run_end=None;started=time.perf_counter()
    truncated = False
    with gzip.open(source,'rb') as stream:
        while True:
            try:
                line = stream.readline()
            except EOFError:
                truncated = True
                break
            if not line:break
            envelope=msgspec.json.decode(line);row=envelope['payload']
            if envelope.get('event')!='replay_input':raise ValueError('unexpected envelope')
            rows+=1
            if row['sequence']!=rows or (previous is not None and row['previousHash']!=previous):
                raise ValueError('source sequence/hash-link discontinuity')
            previous=row['hash'];kind=row['kind'];counts[kind]+=1
            first=row['processingWallSeconds'] if first is None else first
            last=row['processingWallSeconds']
            if kind in KINDS and run_end is None:
                if row['hash'] != fingerprint({k:v for k,v in row.items() if k!='hash'}):
                    raise ValueError('semantic source content hash mismatch')
                db.execute('INSERT INTO inputs VALUES (?,?,?)',(rows,kind,zlib.compress(msgspec.json.encode(row),1)))
                selected[kind]+=1
            if kind=='run_end' and run_end is None:run_end=dict(sequence=rows,wall=last,body=row['body'])
            if rows%1000000==0:
                db.commit();print(json.dumps(dict(rows=rows,selected=sum(selected.values()),seconds=round(time.perf_counter()-started,1))),flush=True)
    db.commit();db.close()
    report=dict(source=str(source.resolve()),compressed_sha256=sha256_file(source),rows=rows,counts=dict(counts),
        selected=dict(selected),run_end=run_end,first_wall=first,last_wall=last,
        gzip_crc_complete=not truncated,compressed_tail_truncated=truncated,hash_chain_links_contiguous=True,
        selected_row_content_hashes_recomputed=True,all_row_content_hashes_recomputed=False,
        complete_run_end=run_end is not None,footer_present=counts['footer']>0,
        scope='causal semantic index, not original scheduler/full replay certification',elapsed_seconds=time.perf_counter()-started)
    (output/'index-manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');a=p.parse_args();index(a.source,a.output)
