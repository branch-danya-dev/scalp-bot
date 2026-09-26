"""Read-only entry diagnostics. Source journals are never opened for writing."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import csv
import gzip
import hashlib
import json
from math import isclose
from pathlib import Path

import msgspec
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scalp_bot.domain import OrderBook
from scalp_bot.execution_book import coherent_execution_book


def consistent_depth(fast, deep):
    """Diagnostic quantity sweeps use the very same fast-head execution rule."""
    return coherent_execution_book(OrderBook(*fast.prices()), OrderBook(*deep.prices()))


HORIZONS = (5, 15, 30, 60, 120)


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)


def prepare(inventory_path, output):
    inv = json.loads(inventory_path.read_text(encoding='utf-8'))
    opens = [e for e in inv['events'] if e['row']['event'] == 'trade_opened']
    closes = {e['row']['payload']['setupId']: e for e in inv['events'] if e['row']['event'] == 'trade_closed'}
    trades = []
    for number, opened in enumerate(opens, 1):
        p = opened['row']['payload']; plan = p['plan']; pos = p['position']
        closed = closes[plan['setup_id']]
        trades.append(dict(id=f'T{number:02}', opened=opened, closed=closed,
            setup=plan['setup_id'], symbol=plan['symbol'], side=plan['side'],
            strategy=plan['strategy'], quantity=pos['original_quantity'],
            entry=pos['entry'], fillMono=pos['opened_mono'],
            closeMono=closed['row']['payload']['market']['clock']['monotonic_seconds'],
            decisions=[], transitions=[], rejects=[]))
    wanted = {t['setup']: t for t in trades}
    offset = 0
    clock_reasons = Counter()
    with Path(inv['source']).open('rb') as stream:
        for line, raw in enumerate(stream, 1):
            at = offset; offset += len(raw)
            r = msgspec.json.decode(raw); p = r['payload']; kind = r['event']
            if kind == 'clock_admission_changed':
                clock_reasons[str(p.get('reason'))] += 1
            if kind not in ('decision','strategy_state_transition','risk_reject','arbiter_blocked'):
                continue
            decision = p.get('decision', p)
            setup = decision.get('setup_id',p.get('setupId'))
            t = wanted.get(setup)
            if t is None or line > t['opened']['line']:
                continue
            if kind == 'decision':
                bucket = 'decisions'
                data = p
            elif kind == 'strategy_state_transition':
                bucket = 'transitions'; data = p
            else:
                bucket = 'rejects'; data = {k:v for k,v in p.items() if k != 'market'}
            t[bucket].append(dict(line=line, offset=at, ts=r['ts'], event=kind, payload=data))
    for t in trades:
        ready = [r for r in t['decisions'] if r['payload'].get('action') == t['side']]
        t['firstReady'] = ready[0] if ready else None
        t['lastReady'] = ready[-1] if ready else None
        # Clock conversion is needed only when a latency trace is absent. Use
        # local wall/monotonic pair from the SAME recorded market snapshot.
        clock = t['opened']['row']['payload']['market']['clock']
        t['wallMonoOffset'] = clock['monotonic_seconds'] - clock['local_wall_ms']/1000
        first = t['firstReady']
        if first:
            t['signalMono'] = first['ts'] + t['wallMonoOffset']
            t['signalTimingSource'] = 'emitted_decision_local_wall_to_snapshot_monotonic'
        else:
            t['signalMono'] = None
    save(output, dict(source=inv['source'], sourceSha256=inv['sha256'], trades=trades,
                      clockReasons=clock_reasons))
    print('Prepared', len(trades), 'trades')


class Book:
    """Same snapshot/update/sequence rules as bybit.OrderBookState, no services."""
    def __init__(self, depth):
        self.depth = depth
        self.clear()

    def clear(self):
        self.b = {}; self.a = {}; self.u = self.seq = None
        self.synced = False; self.receipt = self.exchange = None
        self.line = None

    def update(self, body, line):
        data = body['data']; u = int(data.get('u') or 0); seq = int(data.get('seq') or 0)
        if body['type'] == 'snapshot' or u == 1:
            self.clear(); self.synced = True
        elif not self.synced:
            return 'unsynced'
        elif (self.u and u and u <= self.u) or (self.seq and seq and seq < self.seq):
            # Runtime updates receipt metadata even for a duplicate/old update.
            self.receipt = body['receipt_mono_ns']/1e9
            self.exchange = body.get('cts') or body.get('ts')
            return 'duplicate'
        elif self.u and u > self.u + 1:
            self.clear(); return 'gap'
        for key, levels in (('b',self.b),('a',self.a)):
            for price, quantity in data.get(key, []):
                price, quantity = float(price), float(quantity)
                if quantity: levels[price] = quantity
                else: levels.pop(price, None)
        self.u = u or self.u; self.seq = seq or self.seq
        self.receipt = body['receipt_mono_ns']/1e9
        self.exchange = body.get('cts') or body.get('ts'); self.line = line
        return 'applied'

    def prices(self):
        return sorted(self.b.items(), reverse=True)[:self.depth], sorted(self.a.items())[:self.depth]


def sweep(levels, quantity):
    remain = quantity; value = 0.0
    for price, available in levels:
        fill = min(remain, available); value += fill * price; remain -= fill
        if remain <= 1e-9: return value/quantity
    return None  # Never invent liquidity beyond visible depth.


def quote_at(samples, times, moment):
    i=bisect_right(times,moment)-1
    if i<0: return None
    r=samples[i]
    if (not r['healthy'] or moment-r['deepReceipt']>1.5 or moment-r['fastReceipt']>1.5
            or r['bid'] is None or r['ask'] is None): return None
    return r


def markout(sign, quantity, entry, paid_entry_fee, raw_exit, exit_fee_rate, exit_slippage_rate):
    # Entry is already filled: never subtract its spread/slippage again.
    fill=raw_exit*(1-sign*exit_slippage_rate)
    gross=sign*(raw_exit-entry)*quantity
    slippage=abs(raw_exit-fill)*quantity
    fees=paid_entry_fee+fill*quantity*exit_fee_rate
    return dict(exit_raw_vwap=raw_exit,exit_fill=fill,move_bps=sign*(raw_exit-entry)/entry*10000,
        quote_gross_usd=gross,exit_slippage_usd=slippage,fees_usd=fees,net_usd=gross-slippage-fees)


def extract_inputs(source, prepared, output_dir):
    data = json.loads(prepared.read_text(encoding='utf-8')); trades = data['trades']
    symbols = {t['symbol'] for t in trades}; books = {(s,d):Book(d) for s in symbols for d in (50,1000)}
    windows = {}
    for symbol in symbols:
        rows = [t for t in trades if t['symbol']==symbol]
        windows[symbol] = (min(min(t['signalMono'] or t['fillMono'],t['fillMono']) for t in rows)-2,
                           max(max(t['closeMono'],t['fillMono']+120) for t in rows)+120)
    snapshots = {t['id']: [] for t in trades}
    counts = Counter(); errors = []; previous_hash = None; previous_seq = 0
    hash_kinds={'market_message','transport','bootstrap','manifest','control','run_end','service'}
    checked_hashes=0
    through={t['id']:[] for t in trades}; controls=[]
    previous_mono = 0; n = 0; last = None; footer = False
    traces = {t['opened']['row']['payload']['plan']['strategy_details'].get('latencyTrace',{}).get('eventId'):t
              for t in trades}
    traces.pop(None, None)
    signal_times = {round(t['firstReady']['ts']*1e5):t for t in trades if t['firstReady']}
    try:
        with gzip.open(source,'rb') as stream:
            for n, raw in enumerate(stream,1):
                r = msgspec.json.decode(raw)['payload']; kind = r['kind']; counts[kind]+=1
                if kind in hash_kinds:
                    # stdlib float decoding matches the production fingerprint.
                    original=json.loads(raw)['payload']
                    canonical=json.dumps({k:v for k,v in original.items() if k!='hash'},
                        sort_keys=True,separators=(',',':'),allow_nan=False).encode()
                    if hashlib.sha256(canonical).hexdigest()!=r['hash']:
                        errors.append(dict(line=n,error='content_hash'))
                    checked_hashes+=1
                if r['sequence'] != previous_seq+1 or (previous_hash is not None and r['previousHash'] != previous_hash):
                    errors.append(dict(line=n, error='sequence_or_link'))
                if r['processingMonoNs'] < previous_mono:
                    errors.append(dict(line=n,error='processing_monotonic_rollback'))
                previous_seq=r['sequence']; previous_hash=r['hash']; previous_mono=r['processingMonoNs']; last=r
                if kind=='footer': footer=True
                symbol = r.get('symbol'); body=r['body']; now=r['processingMonoNs']/1e9
                if kind in ('control','run_end','service'): controls.append(dict(line=n,payload=r))
                if kind=='clock_read' and body.get('method')=='time' and round(body.get('value',0)*1e5) in signal_times:
                    t=signal_times.pop(round(body['value']*1e5))
                    t['signalMono']=now
                    t['signalTimingSource']='input_clock_read_at_emitted_decision'
                    t['signalInputLine']=n
                if kind=='transport' and symbol in symbols:
                    # Runtime transport snapshots are recorded after reset.
                    for depth,key in ((50,'fastState'),(1000,'deepState')):
                        state=body.get(key)
                        if state is not None and not state['synced']:
                            books[symbol,depth].clear()
                            if windows[symbol][0]<=now<=windows[symbol][1]:
                                for t in trades:
                                    if t['symbol']==symbol:
                                        snapshots[t['id']].append(dict(mono=now,inputLine=n,deepLine=None,
                                            deepReceipt=None,fastReceipt=None,healthy=False,bid=None,ask=None,
                                            bestBid=None,bestAsk=None,fastBestBid=None,fastBestAsk=None,
                                            reason='transport_not_synced'))
                if kind!='market_message' or symbol not in symbols: continue
                matched=traces.get(body.get('event_id'))
                if matched:
                    trace=matched['opened']['row']['payload']['plan']['strategy_details'].get('latencyTrace',{})
                    matched['triggerInput']=dict(line=n, mono=now, body=body)
                    if trace.get('fireTsNs'):
                        matched['selectedFireMono']=(body['receipt_mono_ns'] + trace['fireTsNs']-trace['receiptTsNs'])/1e9
                topic=body['topic'].split('.')
                if topic[0]=='publicTrade':
                    for t in trades:
                        if t['symbol']!=symbol or not t['fillMono']<=now<=t['fillMono']+120: continue
                        limit=t['opened']['row']['payload']['plan']['strategy_details']['economics']['firstTakePrice']
                        sign=1 if t['side']=='long' else -1
                        for tick in body['data']:
                            if sign*(float(tick['p'])-limit)>=0:
                                through[t['id']].append(dict(inputLine=n,mono=now,price=float(tick['p']),
                                    size=float(tick['v']),aggressor=tick['S'],exchangeMs=tick['T']))
                if topic[0]!='orderbook': continue
                depth=int(topic[1])
                if depth not in (50,1000): continue
                outcome=books[symbol,depth].update(body,n)
                counts['book_'+outcome]+=1
                if not windows[symbol][0] <= now <= windows[symbol][1]: continue
                fast,deep=books[symbol,50],books[symbol,1000]
                coherent = consistent_depth(fast, deep)
                b,a=coherent.bids,coherent.asks
                healthy=(fast.synced and deep.synced and bool(b) and bool(a) and bool(fast.b) and bool(fast.a)
                         and fast.receipt is not None and deep.receipt is not None
                         and now-fast.receipt<=1.5 and now-deep.receipt<=1.5
                         and max(0,(fast.exchange-deep.exchange)/1000)<=.5 and b[0][0]<a[0][0])
                for t in trades:
                    if t['symbol']!=symbol: continue
                    snapshots[t['id']].append(dict(mono=now, inputLine=n, deepLine=deep.line,
                        executionModel="paper-v2-fast-head", executionQuality=coherent.execution,
                        deepReceipt=deep.receipt, fastReceipt=fast.receipt, healthy=healthy,
                        bid=sweep(b,t['quantity']) if healthy else None,
                        ask=sweep(a,t['quantity']) if healthy else None,
                        bestBid=b[0][0] if b else None,bestAsk=a[0][0] if a else None,
                        fastBestBid=max(fast.b) if fast.b else None,fastBestAsk=min(fast.a) if fast.a else None))
                if n%1000000==0: print('Inputs processed',n,flush=True)
    except (EOFError,OSError,msgspec.DecodeError) as exc:
        errors.append(dict(line=n,error=type(exc).__name__,message=str(exc)))
    info=dict(source=str(source), executionModel="paper-v2-fast-head", bytes=source.stat().st_size, inputRows=n, counts=counts,
        footer=footer, errors=errors, lastKind=last['kind'] if last else None,
        lastMono=last['processingMonoNs'] if last else None,
        payloadHashesRecomputed=False, payloadHashCheckedKinds=sorted(hash_kinds),
        payloadHashesChecked=checked_hashes,sequenceAndPreviousHashLinksChecked=True)
    save(output_dir/'input-integrity.json',info)
    save(output_dir/'trades-with-inputs.json',data)
    save(output_dir/'trade-through-and-controls.json',dict(through=through,controls=controls))
    for key,rows in snapshots.items():
        with gzip.open(output_dir/f'{key}-books.jsonl.gz','xt',encoding='utf-8') as stream:
            for row in rows: stream.write(json.dumps(row)+'\n')
    print(json.dumps(info,ensure_ascii=False)); print('Book samples',{k:len(v) for k,v in snapshots.items()})


def calculations(directory, output_csv):
    data=json.loads((directory/'trades-with-inputs.json').read_text(encoding='utf-8'))
    csv_rows=[]; results=[]
    for t in data['trades']:
        with gzip.open(directory/f"{t['id']}-books.jsonl.gz",'rt',encoding='utf-8') as stream:
            samples=[json.loads(line) for line in stream]
        times=[r['mono'] for r in samples]
        sign=1 if t['side']=='long' else -1
        close_side='bid' if sign==1 else 'ask'; entry_side='ask' if sign==1 else 'bid'
        opened=t['opened']['row']['payload']; plan=opened['plan']; details=plan['strategy_details']
        econ=details['economics']; closed=t['closed']['row']['payload']; q=t['quantity']
        fee=econ['stopExitFeeRate']; slip=econ['stopExitSlippageRate']
        entry_fee=opened['position']['entry_fee_total_usd']
        def asof(moment):
            return quote_at(samples,times,moment)
        def diagnostic(book, entry, paid_entry_fee):
            return markout(sign,q,entry,paid_entry_fee,book[close_side],fee,slip)
        fill_book=asof(t['fillMono']); signal_book=asof(t['signalMono'])
        signal_entry=(signal_book[entry_side]*(1+sign*econ['entrySlippageRate']) if signal_book else None)
        result=dict(id=t['id'], symbol=t['symbol'], strategy=t['strategy'],side=t['side'],
            setup=t['setup'], first_signal_line=t['firstReady']['line'], open_line=t['opened']['line'],
            close_line=t['closed']['line'], signal_to_fill_seconds=t['fillMono']-t['signalMono'],
            signal_timing=t['signalTimingSource'], fill_entry=t['entry'],signal_entry=signal_entry,
            fill_reconstruction_error_bps=(10000*(fill_book[entry_side]*(1+sign*econ['entrySlippageRate'])-t['entry'])/t['entry'] if fill_book else None),
            signal_to_fill_entry_drift_bps=(sign*(t['entry']-signal_entry)/signal_entry*10000 if signal_entry else None),
            actual_net=closed['netPnl'],actual_gross=closed['grossPnl'],actual_fees=closed['fees'],
            actual_mfe_bps=closed['maxFavorableMoveBps'],actual_duration=t['closeMono']-t['fillMono'],
            partial_taken=closed['partialTaken'],horizons=[])
        if fill_book and not isclose(fill_book[entry_side]*(1+sign*econ['entrySlippageRate']),t['entry'],rel_tol=1e-10):
            raise ValueError(f"{t['id']}: recorded entry cannot be reconstructed from the input book")
        first_take=econ['firstTakePrice']; original_stop=closed['initialStop']
        quote_key='fastBestBid' if sign==1 else 'fastBestAsk'
        path=[r for r in samples if t['fillMono']<=r['mono']<=t['fillMono']+120
              and r.get(quote_key) is not None and r['fastReceipt'] is not None and r['mono']-r['fastReceipt']<=1.5]
        hit=lambda predicate: next((r for r in path if predicate(r[quote_key])),None)
        take_hit=hit(lambda p:sign*(p-first_take)>=0)
        stop_hit=hit(lambda p:sign*(p-original_stop)<=0)
        result['first_take_quote_seconds']=take_hit['mono']-t['fillMono'] if take_hit else None
        result['initial_stop_quote_seconds']=stop_hit['mono']-t['fillMono'] if stop_hit else None
        result['first_take_before_initial_stop_120s']=bool(take_hit and (not stop_hit or take_hit['mono']<stop_hit['mono']))
        result['quote_path_invalid_samples_120s']=sum(not r['healthy'] for r in samples if t['fillMono']<=r['mono']<=t['fillMono']+120)
        for anchor,moment,entry,paid_entry_fee in (
            ('signal',t['signalMono'],signal_entry,signal_entry*q*econ['entryFeeRate'] if signal_entry else None),
            ('fill',t['fillMono'],t['entry'],entry_fee),
            ('after_close',t['closeMono'],t['entry'],entry_fee)):
            for horizon in HORIZONS:
                target=moment+horizon; book=asof(target)
                row=dict(trade_id=t['id'],symbol=t['symbol'],strategy=t['strategy'],side=t['side'],
                    anchor=anchor,horizon_seconds=horizon,status='ok' if book and entry is not None else 'missing_or_stale_book',
                    entry_reference=entry,quantity=q,at_mono=target,
                    actual_net_usd=closed['netPnl'],actual_mfe_bps=closed['maxFavorableMoveBps'],
                    after_actual_close=target>t['closeMono'],source_session=Path(data['source']).as_posix(),
                    source_inputs=Path(data['source']).with_suffix('.inputs.jsonl.gz').as_posix(),
                    signal_line=result['first_signal_line'],open_line=result['open_line'],close_line=result['close_line'])
                if book and entry is not None:
                    row.update(diagnostic(book,entry,paid_entry_fee),
                        book_input_line=book['inputLine'],depth_input_line=book['deepLine'],
                        book_age_seconds=target-book['deepReceipt'])
                csv_rows.append(row); result['horizons'].append(row)
        results.append(result)
    fields=list(dict.fromkeys(k for row in csv_rows for k in row))
    with output_csv.open('x',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(csv_rows)
    save(directory/'calculations.json',dict(trades=results))
    for t in results:
        print(t['id'],t['symbol'],'delay',round(t['signal_to_fill_seconds'],4),'entry drift bps',t['signal_to_fill_entry_drift_bps'])
        for anchor in ('signal','fill','after_close'):
            print(anchor,[(r['horizon_seconds'],round(r['move_bps'],2),round(r['net_usd'],3))
                if r['status']=='ok' else (r['horizon_seconds'],'NA') for r in t['horizons'] if r['anchor']==anchor])
        print('take/stop120s',t['first_take_quote_seconds'],t['initial_stop_quote_seconds'],t['quote_path_invalid_samples_120s'])


def gzip_check(path, output):
    """Check gzip completion and bind the immutable compressed bytes by hash."""
    count=0; error=None
    try:
        with gzip.open(path,'rb') as stream:
            while chunk:=stream.read(1024*1024): count+=len(chunk)
    except (EOFError,OSError) as exc: error=f'{type(exc).__name__}: {exc}'
    with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
    result=dict(source=str(path),compressedBytes=path.stat().st_size,compressedSha256=digest,
                completeGzip=error is None,decodedBytesBeforeError=count,error=error)
    save(output,result); print(json.dumps(result))


def inventory(path: Path, output: Path):
    size = path.stat().st_size
    digest = hashlib.sha256()
    counts = Counter()
    examples = {}
    events = []
    offset = 0
    bad = []
    first_ts = last_ts = None
    interesting = {'bot_started', 'run_summary', 'bot_stopped', 'trade_opened',
                   'trade_closed', 'partial_take', 'funding_payment',
                   'entry_order_placed', 'entry_order_filled', 'entry_order_cancelled',
                   'strategy_toggle', 'strategy_error', 'arbiter_error'}
    with path.open('rb') as stream:
        for line_no, raw in enumerate(iter(lambda: stream.readline(size - stream.tell()), b''), 1):
            digest.update(raw)
            start = offset
            offset += len(raw)
            try:
                row = msgspec.json.decode(raw)
            except Exception:
                bad.append(dict(line=line_no, offset=start, bytes=len(raw)))
                continue
            event = row['event']
            counts[event] += 1
            first_ts = row['ts'] if first_ts is None else first_ts
            last_ts = row['ts']
            examples.setdefault(event, dict(line=line_no, offset=start,
                payloadKeys=list((row.get('payload') or {}).keys())))
            if event in interesting:
                events.append(dict(line=line_no, offset=start, row=row))
    result = dict(source=str(path), bytes=size, bytesAfter=path.stat().st_size,
                  sha256=digest.hexdigest(), firstTs=first_ts, lastTs=last_ts,
                  counts=dict(counts), examples=examples, badRows=bad, events=events)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(dict(source=str(path), bytes=size, counts=counts, badRows=bad,
        summaries=[e for e in events if e['row']['event']=='run_summary'],
        trades=[dict(line=e['line'], **{k:e['row']['payload'].get(k) for k in
            ('symbol','strategy','side','netPnl','maxFavorableMoveBps','reason','partialTaken')})
            for e in events if e['row']['event']=='trade_closed']), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--prepare', type=Path)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--calculate', type=Path)
    parser.add_argument('--gzip-check', type=Path)
    args = parser.parse_args()
    if args.inventory: inventory(args.source, args.inventory)
    elif args.prepare: prepare(args.source, args.prepare)
    elif args.inputs: extract_inputs(args.source,args.inputs,args.output_dir)
    elif args.calculate: calculations(args.source,args.calculate)
    elif args.gzip_check: gzip_check(args.source,args.gzip_check)
    else: parser.error('choose --inventory, --prepare, --inputs, --calculate or --gzip-check')
