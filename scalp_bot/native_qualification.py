"""Read-only N3 readiness audit of COMPLETE preserved native populations.

This is deliberately not a six-variant runner: v4 journals lack the research
dispatch/clock ownership required by that runner. No logical replay, source
rebinding, clock filtering, network access, or latency acceptance occurs here.
"""
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import msgspec

from .capture_replay import file_sha256
from .input_journal import validate_input_journal
from .manifest_validation import fingerprint
from .pipeline_evidence import quantiles
from .research_journal import read_research
from .run_manifest import code_provenance, replay_config_differences, runtime_provenance


VARIANTS = (
    ('A', 'ordinary core'), ('B', '+ SegmentRegistry shadow'),
    ('C', '+ CrossVenue capture/context'), ('D', '+ MakerShadow'),
    ('E', '+ prepared/label hooks'), ('F', '+ V2 load probe/worker/relay'),
)
SCHEDULER_KINDS = frozenset({'scope', 'scheduler', 'dispatch', 'source_await',
    'context_await', 'service', 'transport', 'control'})


def binding_report(manifest, source_root):
    source = code_provenance(source_root)
    current_runtime = fingerprint(runtime_provenance())
    try:
        differences = replay_config_differences(manifest['config'])
        error = None
    except ValueError as exc:
        differences, error = [], str(exc)
    return dict(recordedSourceSha256=manifest['code']['sourceSha256'],
        candidateSourceSha256=source['sourceSha256'], candidateGitHead=source['gitHead'],
        sourceMatches=source['sourceSha256'] == manifest['code']['sourceSha256'],
        recordedRuntimeSha256=manifest['runtimeSha256'], candidateRuntimeSha256=current_runtime,
        runtimeMatches=manifest['runtimeSha256'] == current_runtime,
        configSha256=manifest['configSha256'], configRoundtripDifferences=differences,
        configRoundtripError=error, configRoundtripMatches=not differences and error is None)


def archive_binding(manifest, freeze, archive):
    """Verify bytes without importing or executing archived code."""
    receipt = json.loads(Path(freeze).read_text(encoding='utf-8'))
    with zipfile.ZipFile(archive) as source:
        expected = dict(manifest['code']['fileHashes'])
        expected.update(receipt['scripts'])
        matched = all(hashlib.sha256(source.read(name)).hexdigest() == digest
                      for name, digest in expected.items())
    return dict(filesMatched=matched, files=len(expected), scripts=receipt['scripts'],
        archiveSha256=file_sha256(archive), freezeSha256=file_sha256(freeze),
        archiveMatchesFreeze=file_sha256(archive) == receipt['sourceArchiveSha256'],
        sourceMatchesFreeze=receipt['source']['sourceSha256'] == manifest['code']['sourceSha256'],
        configMatchesFreeze=receipt['configSha256'] == manifest['configSha256'],
        runtimeMatchesFreeze=receipt['runtimeSha256'] == manifest['runtimeSha256'])


def primary_inventory(path, database):
    """Read every row in sequence; hashes include full rows, never source filters."""
    counts, clocks, scopes = Counter(), Counter(), {}
    hashes = {k: hashlib.sha256() for k in ('population', 'clock', 'scheduler')}
    manifest = last = None
    examples = []
    unowned = 0
    for line in gzip.open(path, 'rb'):
        row = msgspec.json.decode(line)['payload']
        kind, body = row['kind'], row['body']
        counts[kind] += 1
        identity = bytes.fromhex(row['hash'])
        hashes['population'].update(identity)
        if kind in SCHEDULER_KINDS:
            hashes['scheduler'].update(identity)
        if kind == 'manifest' and body['phase'] == 'capture':
            manifest = body['manifest']
        if kind == 'scope':
            if body['phase'] == 'begin': scopes[body['id']] = body['name']
            else: scopes.pop(body['id'], None)
        if kind == 'clock_read':
            hashes['clock'].update(identity)
            clocks[scopes.get(body['scopeId'], '<outside-scope>')] += 1
            unowned += int('owner' not in body)
            if body['scopeId'] is None and len(examples) < 5:
                examples.append(dict(sequence=row['sequence'], method=body['method']))
        if kind == 'market_message':
            database.execute('INSERT INTO market VALUES (?, ?, ?)',
                (row['sequence'], row['symbol'], msgspec.json.encode(body)))
        last = row
    database.commit()
    if manifest is None or last is None:
        raise ValueError('native capture manifest/population missing')
    return dict(counts=dict(counts), rows=sum(counts.values()), lastSequence=last['sequence'],
        lastHash=last['hash'], footerPresent=last['kind'] == 'footer',
        hashes={k: v.hexdigest() for k, v in hashes.items()},
        hashBasis='SHA256 concatenated 32-byte verified row hashes in original order',
        clocksByScope=dict(clocks), clocksWithoutModuleOwner=unowned,
        outsideScopeClockExamples=examples), manifest


def supplemental_inventory(path):
    counts, external, unanchored = Counter(), Counter(), Counter()
    population = hashlib.sha256()
    capture_id = None
    for row in read_research(path):  # Verifies every hash, sequence and footer.
        counts[row['kind']] += 1
        population.update(bytes.fromhex(fingerprint(row)))
        capture_id = row['captureId']
        body = row['body']
        venue = body.get('venue') or body.get('event', {}).get('venue')
        if venue in ('binance', 'okx'):
            external[row['kind']] += 1
            if 'primarySequence' not in body:
                unanchored[row['kind']] += 1
    return dict(counts=dict(counts), rows=sum(counts.values()), captureId=capture_id,
        populationSha256=population.hexdigest(), externalCounts=dict(external),
        externalWithoutPrimaryOrder=dict(unanchored), chainComplete=True)


def joined_native_traces(session, database, output):
    """Join only explicit source references, never nearest timestamps.

    Full features and forecasts are processed. Missing joins remain explicit.
    Data-to-feature includes time until a later evaluation; it is not callback
    compute time. IPC subdivisions cannot be recovered from these old captures.
    """
    counts, spans = Counter(), defaultdict(list)
    joins = Counter()
    digest = hashlib.sha256()
    with Path(session).open('rb') as stream, Path(output).open('xb') as sink:
        for line in stream:
            event = msgspec.json.decode(line)
            kind, body = event['event'], event['payload']
            counts[kind] += 1
            if kind == 'shadow_features':
                ref = body['source']
                key = (ref['symbol'], ref['selection_epoch'], ref['source_sequence'])
                database.execute('INSERT INTO features VALUES (?, ?, ?, ?)', (*key, body['featureEndNs']))
            elif kind == 'shadow_forecast':
                forecast = body['forecast']
                ref = forecast['source']
                key = (ref['symbol'], ref['selection_epoch'], ref['source_sequence'])
                market = database.execute('SELECT symbol, body FROM market WHERE seq=?', (key[2],)).fetchone()
                feature = database.execute('SELECT ready FROM features WHERE symbol=? AND epoch=? AND seq=?', key).fetchone()
                raw = msgspec.json.decode(market[1]) if market else {}
                matched = bool(market and market[0] == key[0] and
                    raw['receipt_mono_ns'] == ref['available_mono_ns'])
                row = dict(capture=Path(session).name, modelCapture=ref['capture_id'], symbol=key[0],
                    epoch=key[1], sourceSequence=key[2], side=forecast['side'],
                    eventId=raw.get('event_id') if matched else None,
                    sourceJoin='MET' if matched else 'NOT_MET', featureJoin='MET' if feature else 'NOT_MET',
                    availableNs=ref['available_mono_ns'], predictionEndNs=forecast['produced_mono_ns'],
                    adapterReceiveNs=body['receivedNs'], adapterEndNs=body['adapterEndNs'])
                joins['forecasts'] += 1
                joins['sourceMatched' if matched else 'sourceMissingOrMismatched'] += 1
                joins['featureMatched' if feature else 'featureMissing'] += 1
                pairs = {}
                if matched:
                    pairs.update(receive_to_parse=(raw['receipt_mono_ns'],raw['parsed_mono_ns']),
                        parse_to_callback=(raw['parsed_mono_ns'],raw['processor_started_mono_ns']),
                        data_to_adapter=(ref['available_mono_ns'],body['adapterEndNs']))
                if matched and feature:
                    row['featureReadyNs'] = feature[0]
                    pairs.update(data_to_features=(ref['available_mono_ns'],feature[0]),
                        features_to_prediction_end=(feature[0],forecast['produced_mono_ns']),
                        prediction_end_to_receipt=(forecast['produced_mono_ns'],body['receivedNs']),
                        adapter=(body['receivedNs'],body['adapterEndNs']))
                durations = {}
                for name, (left, right) in pairs.items():
                    if left > 0 and right >= left:
                        durations[name] = (right-left)/1e6
                        spans[name].append(durations[name])
                    else:
                        joins['invalidSpan:'+name] += 1
                row['durationsMs'] = durations
                if all(k in durations for k in ('data_to_features', 'features_to_prediction_end',
                        'prediction_end_to_receipt', 'adapter', 'data_to_adapter')):
                    # Per-item telescoping integer timestamps, NOT a sum of p99s.
                    row['criticalPathNs'] = [feature[0]-ref['available_mono_ns'],
                        forecast['produced_mono_ns']-feature[0],
                        body['receivedNs']-forecast['produced_mono_ns'],body['adapterEndNs']-body['receivedNs']]
                    row['criticalPathExact'] = sum(row['criticalPathNs']) == body['adapterEndNs']-ref['available_mono_ns']
                    joins['criticalPathsExact'] += int(row['criticalPathExact'])
                encoded = msgspec.json.encode(row)+b'\n'
                sink.write(encoded); digest.update(encoded)
    return dict(counts=dict(counts), joins=dict(joins), tracesSha256=digest.hexdigest(),
        stagesMs={k: quantiles(v) for k, v in spans.items()},
        missingStages=['enqueue', 'callback_end', 'probe_poll_dispatch', 'worker_start',
            'request_ipc', 'reply_ipc', 'relay_enqueue', 'relay_dwell', 'prepared_to_plan_to_fire'],
        scope='recorded native observations; no new replay timing and no sum of marginal p99s')


def audit(primary, supplemental, session, output, *, source_root, freeze=None, archive=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schema='n3-native-readiness-v1', controlledGate='NOT_MET',
        sixVariantGate='NOT_TESTED', nativeLabelGate='INCONCLUSIVE',
        marketAuthorized=False, trainingAuthorized=False, demoAuthorized=False,
        blockers=[], variants=[dict(id=k, cumulativePath=v, status='NOT_TESTED',
            ordinaryDecisionSha256=None, portfolioSha256=None, measuredLatency=None) for k,v in VARIANTS])
    database = sqlite3.connect(output/'source-joins.sqlite')
    database.execute('PRAGMA cache_size=-8192')
    database.execute('CREATE TABLE market (seq INTEGER PRIMARY KEY, symbol TEXT, body BLOB)')
    database.execute('CREATE TABLE features (symbol TEXT, epoch INTEGER, seq INTEGER, ready INTEGER, PRIMARY KEY(symbol,epoch,seq))')
    try:
        report['files'] = {label: dict(path=str(Path(path).resolve()),bytes=Path(path).stat().st_size,
            sha256=file_sha256(path)) for label,path in (('primary',primary),('supplemental',supplemental),('session',session))}
        print('Validating complete native primary chain/scopes...', flush=True)
        report['integrity'] = validate_input_journal(primary)
        population, manifest = primary_inventory(primary, database)
        report['primary'] = population
        report['binding'] = binding_report(manifest, source_root)
        if freeze is not None and archive is not None:
            report['archiveBinding'] = archive_binding(manifest,freeze,archive)
        report['supplemental'] = supplemental_inventory(supplemental)
        report['nativeTraces'] = joined_native_traces(session,database,output/'native-joined-traces.jsonl')
        if report['integrity']['structuralStatus'] != 'checks_passed':
            report['blockers'].append('primary_chain_or_scope_invalid')
        if report['supplemental']['captureId'] != Path(session).name:
            report['blockers'].append('supplemental_capture_identity_mismatch')
        for key in ('sourceMatches','runtimeMatches','configRoundtripMatches'):
            if not report['binding'][key]: report['blockers'].append(key+':false')
        report['blockers'] += [
            'v4_clock_reads_have_no_module_owner; A-E cannot skip or attribute them safely',
            'external_supplemental_dispatch_lacks_total_primary_order',
            'V2_probe_poll_and_worker_relay_schedule_not_in_native_dispatch_tape',
            'six_variant_native_executor_not_implemented; strict ordinary scheduler alone is insufficient',
            'native_replay_wall_cost_is_not_native_event_loop_or_real_IPC_latency',
        ]
    except Exception as exc:
        report['failure'] = dict(type=type(exc).__name__,reason=str(exc))
        report['blockers'].append('audit_failed')
    finally:
        database.close()
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report
