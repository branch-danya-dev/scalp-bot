"""Freeze the bot's actual shared context; labels consume later executable books."""
from collections import Counter, defaultdict
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

from ..domain import Side
from .contracts import SnapshotRef
from .dataset import PendingLabel, POLICY_PATH, temporal_split
from .features import FEATURE_NAMES, FEATURE_SCHEMA, ContextCoverage, extract_context_features
from .history.importer import sha256_file


class EngineDatasetCollector:
    def __init__(self, output, capture_id):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=False)
        self.capture_id=capture_id
        self.policy=json.loads(POLICY_PATH.read_text())
        self.rows=[];self.pending=defaultdict(list);self.epochs=defaultdict(int)
        self.starts={};self.last_sample={};self.excluded=Counter();self.inventory={};self.last_quote_ns={}

    def invalidate(self,symbol,now):
        self.epochs[symbol]+=1;self.starts[symbol]=now
        self.excluded['transport_gap']+=len(self.pending.pop(symbol,[]))

    def sample(self,engine,session):
        now=engine.clock.perf_counter_ns();symbol=session.symbol
        start=self.starts.setdefault(symbol,now)
        if now-start<self.policy['warmup_seconds']*1e9:return
        bucket=now//int(self.policy['sample_seconds']*1e9)
        context=session.market_context
        if self.last_sample.get(symbol)==bucket or context is None:return
        if session.instrument is None:
            self.excluded['instrument_unavailable']+=1;return
        if not context.execution.ready or not context.execution.book_synced:return
        # Never relabel a cached context with a newer observation time.
        clock=engine._clock_state(session)
        expected=clock['evaluationMs'] if clock else int(engine.clock.time()*1000)
        if context.observed_at_ms!=expected:return
        self.last_sample[symbol]=bucket
        self.inventory[symbol]={'instrument':asdict(session.instrument),'units':'captured linear base quantity; quote USDT','specification_time':'original contemporaneous bootstrap'}
        windows=tuple(n for n in (5,15,60) if now-start>=n*1e9)
        coverage=ContextCoverage(windows,windows,context.forming_candle is not None,
            session.deep_book_is_fresh(expected/1000),context.structure is not None,True)
        ref=SnapshotRef(self.capture_id,symbol,self.epochs[symbol],engine.recorder.sequence,
                       context.observed_at_ms,now,'capture:'+self.capture_id,FEATURE_SCHEMA)
        snapshot=extract_context_features(context,ref,coverage)
        deadline=now+int(self.policy['horizon_seconds']*1e9)
        for side in ('long','short'):
            row=dict(ref=asdict(ref),features=list(snapshot.values),side=side,label_end_ns=deadline,
                     episode=f'{self.capture_id}:{now//60_000_000_000}',
                     snapshot_quote=session.orderbook.executable_entry(Side(side)))
            self.pending[symbol].append(PendingLabel(row,self.epochs[symbol],deadline,
                now+self.policy['latency_ms']*1_000_000))

    def quote(self,engine,symbol):
        now=engine.clock.perf_counter_ns();session=engine.sessions[symbol]
        previous=self.last_quote_ns.get(symbol)
        self.last_quote_ns[symbol]=now
        if previous is not None and now-previous>self.policy['max_book_age_ms']*1_000_000:
            self.invalidate(symbol,now)
        adapter=SimpleNamespace(epoch=self.epochs[symbol],session=session,instrument=session.instrument,
            health_reason=None if session.book_synced and session.orderbook.mid else 'unsynced')
        remaining=[]
        for item in self.pending[symbol]:
            result=item.advance(adapter,now,self.policy)
            if result is None:remaining.append(item)
            elif result.startswith('excluded:'):self.excluded[result]+=1
            else:self.rows.append(item.row)
        self.pending[symbol]=remaining

    def finish(self):
        self.excluded['right_censored']+=sum(map(len,self.pending.values()))
        rows=self.rows
        first=min(r['ref']['available_mono_ns'] for r in rows)
        last=max(r['ref']['available_mono_ns'] for r in rows)
        boundaries=[int(first+(last-first)*part) for part in self.policy['splits']]
        temporal_split(rows,boundaries)
        rows.sort(key=lambda r:(r['ref']['available_mono_ns'],r['ref']['symbol'],r['side']))
        path=self.output/'dataset.jsonl'
        with path.open('x') as f:
            for r in rows:f.write(json.dumps(r,separators=(',',':'))+'\n')
        report=dict(schema_version=2,training_ready=False,feature_schema=FEATURE_SCHEMA,feature_names=FEATURE_NAMES,
            dataset_sha256=sha256_file(path),policy_sha256=sha256_file(POLICY_PATH),plan_policy=self.policy,
            source='actual StudyEngine B contexts after sequential raw event processing',capture_id=self.capture_id,
            evidence_scope='within previously studied session; test is NOT untouched external holdout',
            inventory=self.inventory,split_boundaries_ns=boundaries,labels=dict(Counter(r['label'] for r in rows)),
            splits=dict(Counter(r['split'] for r in rows)),excluded=dict(self.excluded),
            coverage={name:sum(r['features'][i] is not None for r in rows)/len(rows) for i,name in enumerate(FEATURE_NAMES)},
            mask_mean={name:sum(r['features'][i] or 0 for r in rows)/len(rows) for i,name in enumerate(FEATURE_NAMES) if name.endswith('_known')},
            split_classes={s:dict(Counter(r['label'] for r in rows if r['split']==s)) for s in ('train','calibration','validation','test')})
        report['training_ready']=all(report['splits'].get(s,0)>=30 for s in ('train','calibration','validation','test')) and len(report['split_classes']['train'])==3
        (self.output/'manifest.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
