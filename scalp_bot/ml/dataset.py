"""Streaming raw input -> shared context -> frozen features -> executable labels."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib

from ..domain import Side
from .capture_context import CaptureContext
from .features import FEATURE_NAMES, FEATURE_SCHEMA
from .history.importer import sha256_file

POLICY_PATH=Path(__file__).resolve().parents[2]/"docs/ml/experiments/impulse-plan-v1.json"


def read_events(source):
    source=Path(source)
    if source.suffix==".sqlite":
        with sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro",uri=True) as db:
            query="SELECT payload FROM inputs WHERE kind IN ('bootstrap','symbol_lifecycle','transport','market_message','run_end') ORDER BY idx"
            for (body,) in db.execute(query):
                yield json.loads(zlib.decompress(body))
    else:
        with gzip.open(source,"rt",encoding="utf-8") as stream:
            for line in stream:
                row=json.loads(line)
                if row.get("event")=="replay_input":
                    yield row["payload"]


@dataclass
class PendingLabel:
    row: dict
    epoch: int
    deadline: int
    earliest: int
    entry: float | None = None
    quantity: float = 0
    stop: float = 0
    target: float = 0

    def advance(self, adapter, now, policy):
        if adapter.epoch!=self.epoch or adapter.health_reason:
            return "excluded:book_gap"
        if now-self.row["ref"]["available_mono_ns"]<policy["latency_ms"]*1_000_000:
            return None
        side=Side(self.row["side"]); sign=1 if side==Side.LONG else -1
        book=adapter.session.orderbook
        if self.entry is None:
            if now-self.earliest>policy["max_book_age_ms"]*1_000_000:
                return "excluded:entry_latency_gap"
            normalized=adapter.instrument.normalize_quantity(entry_price=book.executable_entry(side),
                requested_notional=policy["nominal_usdt"],market_order=True)
            if normalized is None:
                return "excluded:quantity"
            self.quantity=normalized[0]
            fill,qty,_=book.entry_vwap_quantity(side,self.quantity)
            if fill is None or qty<self.quantity-1e-10:
                return "excluded:entry_depth"
            self.entry=fill*(1+sign*policy["slippage_bps_each_fill"]/10000)
            self.stop=adapter.instrument.stop_price(self.entry*(1-sign*policy["stop_bps"]/10000),side)
            self.target=adapter.instrument.target_price(self.entry*(1+sign*policy["target_bps"]/10000),side)
            self.row.update(entry=self.entry,quantity=self.quantity,stop=self.stop,target=self.target,entry_ns=now)
        quote=book.executable_exit(side)
        if quote is None:
            return "excluded:exit_quote"
        if now>self.deadline+policy["max_book_age_ms"]*1_000_000:
            return "excluded:horizon_gap"
        stopped=sign*(quote-self.stop)<=0
        targeted=sign*(quote-self.target)>=0
        if stopped and targeted:
            return "excluded:ambiguous"
        label="stop_first" if stopped else "target_first" if targeted else "timeout" if now>=self.deadline else None
        if label is None:
            return None
        fill,qty,_=book.exit_vwap_quantity(side,self.quantity)
        if fill is None or qty<self.quantity-1e-10:
            return "excluded:exit_depth"
        exit_price=fill*(1-sign*policy["slippage_bps_each_fill"]/10000)
        gross=sign*(exit_price-self.entry)*self.quantity
        fees=(exit_price+self.entry)*self.quantity*policy["taker_fee_rate"]
        self.row.update(label=label,exit=exit_price,exit_ns=now,gross_usdt=gross,fees_usdt=fees,net_usdt=gross-fees)
        return label


def temporal_split(rows, boundaries):
    """Purge the complete future window and a global 60-second episode bucket."""
    names=("train","calibration","validation","test")
    for row in rows:
        start=row["ref"]["available_mono_ns"]
        end=row["label_end_ns"]
        left=start//60_000_000_000*60_000_000_000
        right=max(end,(start//60_000_000_000+1)*60_000_000_000)
        if any(left<=b<=right for b in boundaries):
            row["split"]="purged"
        else:
            row["split"]=names[sum(start>b for b in boundaries)]
    return rows


def build_dataset(source, output, capture_id, *, max_events=5_000_000):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    policy=json.loads(POLICY_PATH.read_text())
    adapters={};active=set();pending=defaultdict(list);last_sample={};rows=[]
    exclusions=Counter();counts=Counter();inventory={};source_digest=hashlib.sha256()
    first=last=None;processed=0
    for event in read_events(source):
        processed+=1
        if processed>max_events:
            raise ValueError("source event budget exceeded; no complete manifest")
        kind,symbol=event["kind"],event.get("symbol");now=event["processingMonoNs"]
        source_digest.update(json.dumps(event,sort_keys=True,separators=(",",":")).encode())
        counts[kind]+=1
        if kind=="run_end":
            break  # Never consume the incomplete transport teardown as training data.
        if kind=="bootstrap":
            adapters[symbol]=CaptureContext(symbol,capture_id,event["body"],now)
            inventory[symbol]=dict(bootstrap_sequence=event["sequence"],instrument=event["body"]["instrument"],
                units="base asset quantity, linear USDT quote",contract_multiplier=1,
                specification_time="captured contemporaneous bootstrap, not current REST")
        if kind=="symbol_lifecycle":
            if event["body"]["action"]=="activate": active.add(symbol)
            else:
                active.discard(symbol)
                exclusions["deactivated"]+=len(pending.pop(symbol,[]))
        adapter=adapters.get(symbol)
        if adapter is None or kind not in {"market_message","transport"}:
            continue
        old_epoch=adapter.epoch
        quote_updated=adapter.apply(event)
        if adapter.epoch!=old_epoch:
            exclusions["book_gap"]+=len(pending.pop(symbol,[]))
        if not quote_updated:
            continue
        remaining=[]
        for item in pending[symbol]:
            result=item.advance(adapter,now,policy)
            if result is None: remaining.append(item)
            elif result.startswith("excluded:"): exclusions[result]+=1
            else: rows.append(item.row)
        pending[symbol]=remaining
        if symbol not in active or now-adapter.start_ns<policy["warmup_seconds"]*1_000_000_000:
            continue
        bucket=now//(policy["sample_seconds"]*1_000_000_000)
        if last_sample.get(symbol)==bucket:
            continue
        last_sample[symbol]=bucket
        snapshot,context,coverage=adapter.snapshot(now)
        if not context.execution.ready or not context.execution.book_synced:
            exclusions["sample_context_not_ready"]+=1;continue
        first=now if first is None else min(first,now);last=now
        deadline=now+policy["horizon_seconds"]*1_000_000_000
        for side in ("long","short"):
            row=dict(ref=asdict(snapshot.ref),features=list(snapshot.values),side=side,
                label_end_ns=deadline,episode=f"{capture_id}:{now//60_000_000_000}")
            pending[symbol].append(PendingLabel(row,adapter.epoch,deadline,
                                               now+policy["latency_ms"]*1_000_000))
        if processed%100000<10: print(f"events={processed} labels={len(rows)}",flush=True)
    exclusions["right_censored"]+=sum(map(len,pending.values()))
    if first is None or last is None or last<=first:
        raise ValueError("no sufficiently covered samples")
    boundaries=[int(first+(last-first)*part) for part in policy["splits"]]
    temporal_split(rows,boundaries)
    rows.sort(key=lambda r:(r["ref"]["available_mono_ns"],r["ref"]["symbol"],r["side"]))
    dataset=output/"dataset.jsonl"
    with dataset.open("x",encoding="utf-8") as sink:
        for row in rows:sink.write(json.dumps(row,separators=(",",":"))+"\n")
    report=dict(schema_version=1,status="built",training_ready=all(sum(r["split"]==split for r in rows)>=30 for split in ("train","calibration","validation","test")) and {r["label"] for r in rows if r["split"]=="train"}=={"target_first","stop_first","timeout"},
        evidence_scope="technical within-session candidate; no untouched or independent period holdout",
        capture_id=capture_id,source=str(Path(source).resolve()),source_subset_sha256=source_digest.hexdigest(),
        dataset_sha256=sha256_file(dataset),feature_schema=FEATURE_SCHEMA,feature_names=FEATURE_NAMES,
        plan_policy=policy,policy_sha256=sha256_file(POLICY_PATH),inventory=inventory,
        events=dict(counts),period_ns=[first,last],observed_hours=(last-first)/3.6e12,
        labels=dict(Counter(r["label"] for r in rows)),splits=dict(Counter(r["split"] for r in rows)),
        split_boundaries_ns=boundaries,excluded=dict(exclusions),
        coverage={name:sum(r["features"][i] is not None for r in rows)/len(rows) for i,name in enumerate(FEATURE_NAMES)},
        split_classes={split:dict(Counter(r["label"] for r in rows if r["split"]==split)) for split in ("train","calibration","validation","test")},
        unknown_groups=["liquidity evidence","structure features"],
        retention="local until owner deletes; no automatic expiry or provider redistribution",
        artifact_path=str(output.resolve()),portfolio_result=None)
    (output/"manifest.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("labels","splits","excluded","observed_hours")}))
    return report
