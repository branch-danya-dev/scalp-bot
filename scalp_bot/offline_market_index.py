"""Derived executable quotes and rolling tape, using production book semantics."""
from collections import defaultdict,deque
import json,sqlite3
from pathlib import Path
from .bybit import OrderBookState,OrderBookSequenceError,MarketMessage
from .domain import TradeTick
from .strategy.flow import prune_trades
from .offline_study import source_events
from .offline_comparison import episodes
from .strategy.rejection_response import assess_reclaim_response


def build(source,runs,output):
    output=Path(output);output.mkdir(exist_ok=False,parents=True)
    targets=defaultdict(list)
    for variant in 'ABC':
        ep,manifest=episodes(Path(runs)/variant/(variant+'.jsonl'))
        for key,r in ep.items():
            if r['ready_sequence'] and r['reclaim']:
                targets[r['ready_sequence']].append((variant,key,r))
    db=sqlite3.connect(output/'quotes.sqlite');db.execute('CREATE TABLE quotes(symbol TEXT,mono REAL,wall REAL,seq INTEGER,bid REAL,ask REAL)')
    books={};tapes=defaultdict(deque);current={};answers=[];counts=defaultdict(int);buffer=90
    for e in source_events(source):
        if e['kind']=='run_end':break
        symbol,b,seq=e['symbol'],e['body'],e['sequence']
        if e['kind']=='manifest':buffer=b['manifest']['config']['trade_buffer_seconds']
        if e['kind']=='bootstrap':books[symbol]=OrderBookState(50)
        if e['kind']=='transport' and b['phase'] in ('fault','connecting','cancelled') and f'orderbook.50.{symbol}' in b['topics']:
            books[symbol]._clear();tapes[symbol].clear();current.pop(symbol,None)
            db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,e['processingMonoNs']/1e9,e['processingWallSeconds'],seq,None,None))
        if e['kind']=='market_message' and symbol in books:
            if b['topic'].startswith('orderbook.50.'):
                try:
                    book=books[symbol].apply(MarketMessage(**b));current[symbol]=book
                    db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,e['processingMonoNs']/1e9,e['processingWallSeconds'],seq,book.best_bid,book.best_ask))
                    counts['quotes']+=1
                except OrderBookSequenceError:counts['sequence_gap']+=1;current.pop(symbol,None)
            elif b['topic'].startswith('publicTrade.'):
                for t in b['data']:
                    tick=TradeTick(int(t['T']),float(t['p']),float(t['v']),t['S']);tapes[symbol].append(tick);prune_trades(tapes[symbol],tick.ts_ms,buffer)
                    counts['trades']+=1
        for variant,key,r in targets.get(seq,[]):
            symbol=key[0];state=r['reclaim']['payload']['state'];book=current.get(symbol)
            if book and r['ready_market_ms']:
                quote=book.best_ask if key[4]=='long' else book.best_bid
                a=assess_reclaim_response(key[4],state['reclaim_at_ms'],state['reclaim_quote'],quote,list(tapes[symbol]),r['ready_market_ms'],1.5)
                answers.append(dict(variant=variant,key=key,source_sequence=seq,quote=quote,tape_buffer_seconds=buffer,assessment=a))
    db.execute('CREATE INDEX symbol_mono ON quotes(symbol,mono)');db.commit();db.close()
    report=dict(source=str(Path(source).resolve()),counts=dict(counts),method='actual OrderBookState and prune_trades; transport clears book/tape; checkpoint after source sequence',ready_responses=answers)
    (output/'manifest.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(dict(counts)));return report


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('runs');p.add_argument('output');a=p.parse_args();build(a.source,a.runs,a.output)
