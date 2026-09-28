import gzip
import json
from pathlib import Path
import sqlite3

from scalp_bot.manifest_validation import fingerprint
from scalp_bot.native_qualification import primary_inventory, joined_native_traces, binding_report, VARIANTS
from test_offline_bootstrap import prefix


def database():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE market (seq INTEGER PRIMARY KEY, symbol TEXT, body BLOB)')
    db.execute('CREATE TABLE features (symbol TEXT, epoch INTEGER, seq INTEGER, ready INTEGER, PRIMARY KEY(symbol,epoch,seq))')
    return db


def test_inventory_includes_every_clock_scheduler_and_control_in_order(tmp_path):
    rows=prefix()
    for kind,body in [('service',{'phase':'start'}),
        ('scope',dict(phase='begin',id=1,parentId=None,name='evaluate')),
        ('clock_read',dict(scopeId=1,method='time',value=12.0)),
        ('scope',dict(phase='end',id=1,outcome='returned')),
        ('scheduler',dict(phase='scheduled')),('footer',{})]:
        row=dict(sequence=len(rows)+1,kind=kind,symbol=None,body=body,previousHash=rows[-1]['hash'])
        row['hash']=fingerprint(row);rows.append(row)
    path=tmp_path/'inputs.gz'
    with gzip.open(path,'wt') as stream:
        for row in rows:stream.write(json.dumps({'payload':row})+'\n')
    db=database()
    try:
        report,manifest=primary_inventory(path,db)
        assert report['rows']==len(rows) and report['lastHash']==rows[-1]['hash']
        assert report['counts']['clock_read']==1 and report['counts']['scheduler']==1
        assert report['clocksWithoutModuleOwner']==1
        assert report['hashes']['clock'] != report['hashes']['scheduler']
        assert manifest==rows[1]['body']['manifest']
        assert [v[0] for v in VARIANTS]==list('ABCDEF')
    finally: db.close()


def test_bound_native_join_uses_identity_and_telescopes_each_item(tmp_path):
    db=database()
    market=dict(event_id='native-event',receipt_mono_ns=100,parsed_mono_ns=110,processor_started_mono_ns=125)
    db.execute('INSERT INTO market VALUES (7, ?, ?)',('AAA',json.dumps(market).encode()))
    ref=dict(capture_id='v2-probe',symbol='AAA',selection_epoch=3,source_sequence=7,available_mono_ns=100)
    rows=[dict(event='shadow_features',payload=dict(source=ref,featureEndNs=200)),
          dict(event='shadow_forecast',payload=dict(forecast=dict(source=ref,side='long',produced_mono_ns=400),receivedNs=450,adapterEndNs=460)),
          dict(event='shadow_forecast',payload=dict(forecast=dict(source=dict(ref,selection_epoch=4),side='short',produced_mono_ns=400),receivedNs=450,adapterEndNs=460)),
          dict(event='shadow_forecast',payload=dict(forecast=dict(source=dict(ref,available_mono_ns=99),side='short',produced_mono_ns=400),receivedNs=450,adapterEndNs=460))]
    session=tmp_path/'session.jsonl'
    session.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    try:
        report=joined_native_traces(session,db,tmp_path/'joined.jsonl')
        result=[json.loads(line) for line in (tmp_path/'joined.jsonl').read_text().splitlines()]
        assert result[0]['eventId']=='native-event'
        assert result[0]['criticalPathNs']==[100,200,50,10]
        assert result[0]['criticalPathExact']
        assert result[1]['featureJoin']=='NOT_MET' and 'criticalPathExact' not in result[1]
        assert result[2]['sourceJoin']=='NOT_MET' and 'data_to_adapter' not in result[2]['durationsMs']
        assert report['joins']['forecasts']==3
        assert report['joins']['criticalPathsExact']==1
    finally:db.close()


def test_original_numeric_manifest_fault_is_reported_without_rebinding():
    manifest=prefix()[1]['body']['manifest']
    manifest['config']['paper_run_duration_seconds']=300
    original=fingerprint(manifest)
    report=binding_report(manifest,Path(__file__).resolve().parents[1])
    assert not report['configRoundtripMatches']
    assert report['configRoundtripDifferences']==['paper_run_duration_seconds']
    assert fingerprint(manifest)==original
