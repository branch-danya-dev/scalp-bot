"""Read sealed live E01 feeds; retain support for synthetic v1 fixtures."""
import json
from pathlib import Path

from .manifest_schema import PUBLIC_CONFIG_FIELDS
from .manifest_validation import fingerprint
from .run_manifest import code_provenance, runtime_provenance


def read_header(stream):
    header = json.loads(stream.readline())
    version = header.get('schema')
    fields = {'schema', 'config'} if version == 'e01-feed-v1' else {'schema', 'config', 'code', 'runtime'}
    if version == 'paired-feed-v3': fields = fields | {'experiment'}
    if (version not in ('e01-feed-v1', 'e01-feed-v2', 'paired-feed-v3') or set(header) != fields
            or set(header['config']) != PUBLIC_CONFIG_FIELDS):
        raise ValueError('expected E01 feed and complete public configuration')
    if version == 'paired-feed-v3' and header['experiment'] not in ('E01', 'E06'):
        raise ValueError('unsupported paired experiment')
    if version != 'e01-feed-v1':
        current = code_provenance(Path(__file__).resolve().parents[1])
        if header['code']['sourceSha256'] != current['sourceSha256']:
            raise ValueError('E01 capture source differs from current source')
        if fingerprint(header['runtime']) != fingerprint(runtime_provenance()):
            raise ValueError('E01 capture runtime differs from current runtime')
    return header


def observations(stream, header):
    if header['schema'] == 'e01-feed-v1':
        for line in stream:
            yield json.loads(line)
        return
    count, previous, last_kind = 0, fingerprint(header), None
    for line in stream:
        row = json.loads(line)
        if row.get('footer') is True:
            if (row != dict(footer=True, eventsConsumed=count, sharedInputHash=previous)
                    or last_kind != 'stop' or stream.read()):
                raise ValueError('invalid E01 seal or trailing data')
            return
        if (set(row) != {'sequence', 'previousHash', 'observation', 'hash'}
                or type(row['sequence']) is not int or row['sequence'] != count + 1
                or row['previousHash'] != previous
                or row['hash'] != fingerprint(dict(previousHash=previous, observation=row['observation']))):
            raise ValueError('broken E01 input chain')
        count += 1
        previous = row['hash']
        last_kind = row['observation'].get('kind')
        yield row['observation']
    raise ValueError('unsealed E01 capture: missing footer')
