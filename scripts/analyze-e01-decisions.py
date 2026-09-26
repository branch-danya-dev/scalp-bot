"""Describe recorded E01 admissions and subsequent tape; never simulate fills.

Historical data analysis does not execute captured code or require current source
parity. The source archive is checked against the header instead; replay guards
in e01_feed.read_header remain unchanged.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import zipfile

import msgspec

from scalp_bot.e01_feed import observations
from scalp_bot.manifest_validation import fingerprint, check_manifest


HORIZONS = (60, 180, 300)


def identity(symbol, strategy, side, setup):
    if not symbol or not strategy or side not in ('long', 'short') or not setup:
        raise ValueError('missing candidate identity')
    return symbol, strategy, side, setup


def scan_events(path):
    signals, opened, blocked, first_breaks = {}, defaultdict(list), {}, {}
    generations, states = set(), defaultdict(set)
    reasons, actions, counts = Counter(), Counter(), Counter()
    closed = []
    digest = hashlib.sha256()
    start = stop = None
    previous = -1
    with Path(path).open('rb') as stream:
        for line in stream:
            digest.update(line)
            r = msgspec.json.decode(line)
            event, p, mono = r['event'], r['payload'], r['monoNs']
            if mono < previous:
                raise ValueError('nonmonotonic event ledger')
            previous = mono
            counts[event] += 1
            if event == 'bot_started': start = mono
            if event == 'bot_stopped': stop = mono
            if event == 'trade_closed': closed.append(p)
            if start is None or stop is not None:
                continue
            if event == 'decision':
                if p['strategy'] == 'level_breakout':
                    actions[p['action']] += 1
                    if p['action'] == 'wait': reasons.update(p['reasons'])
                    gen = p.get('details', {}).get('zoneGeneration')
                    if gen:
                        key = r['symbol'], json.dumps(gen, sort_keys=True)
                        generations.add(key)
                        states[p['details'].get('state')].add(key)
                        if p['details'].get('state') == 'break' and p['action'] == 'wait':
                            price = p.get('marketContext', {}).get('lastPrice')
                            if not price or gen[0] not in ('support', 'resistance'):
                                raise ValueError('missing first BREAK reference price/direction')
                            first_breaks.setdefault(key, dict(symbol=r['symbol'], strategy='level_breakout',
                                side='short' if gen[0] == 'support' else 'long', generation=gen,
                                monoNs=mono, wallSeconds=r['wallSeconds'], entry=price, stop=None, target=None,
                                structuralPath={}, reasons=p['reasons'],
                                pressure={k:v for k,v in p['details'].get('pressure', {}).items() if k != 'formingCandle'}))
                if p['action'] in ('long', 'short') and all(p.get(k) is not None for k in ('entry', 'stop', 'target')):
                    key = identity(r['symbol'], p['strategy'], p['action'], p['setup_id'])
                    signals.setdefault(key, dict(firstMonoNs=mono, firstWallSeconds=r['wallSeconds']))
            if event == 'trade_opened':
                plan = p['plan']
                key = identity(r['symbol'], plan['strategy'], plan['side'], plan['setup_id'])
                opened[key].append(dict(monoNs=mono, wallSeconds=r['wallSeconds'],
                    entry=p['position']['entry'], notional=plan['notional']))
            if event == 'arbiter_blocked':
                d = p['decision']
                key = identity(r['symbol'], p['strategy'], d['action'], p['setupId'])
                group = blocked.setdefault(key, dict(symbol=r['symbol'], strategy=p['strategy'],
                    side=d['action'], setupId=p['setupId'], monoNs=mono, wallSeconds=r['wallSeconds'],
                    entry=d['entry'], stop=d['stop'], target=d['target'], blockers=set(),
                    structuralPath=p['semanticArbitration']['structuralPath'], eventCount=0))
                group['blockers'].update(p['blockers'])
                group['eventCount'] += 1
    if any(counts[e] != 1 for e in ('bot_started', 'bot_stopped', 'run_summary')):
        raise ValueError('incomplete lifecycle')
    if not set(opened).issubset(signals) or not set(blocked).issubset(signals):
        raise ValueError('missing recorded tradeable decision')
    for b in blocked.values(): b['blockers'] = sorted(b['blockers'])
    summary = dict(fileSha256=digest.hexdigest(), startMonoNs=start, stopMonoNs=stop,
        decisionActions=dict(actions), observedLevelGenerations=len(generations),
        generationsByState={k: len(v) for k, v in states.items()},
        waitReasonEventCounts=dict(reasons.most_common()), closedTrades=len(closed))
    return dict(summary=summary, signals=signals, opened=opened, blocked=blocked, closed=closed, firstBreaks=first_breaks)


class TapeWindow:
    """Observed prints after first rejection; no synthetic entry, fees or exits."""
    def __init__(self, block, seconds):
        self.block, self.seconds = block, seconds
        self.end = block['monoNs'] + seconds * 1_000_000_000
        self.count = 0
        self.minimum = self.maximum = self.last = self.last_ns = None
        self.touches = {}

    def add(self, mono, price):
        if not self.block['monoNs'] < mono <= self.end:
            return
        if not math.isfinite(price) or price <= 0:
            raise ValueError('invalid tape price')
        self.count += 1
        self.minimum = price if self.minimum is None else min(price, self.minimum)
        self.maximum = price if self.maximum is None else max(price, self.maximum)
        if self.last_ns is None or mono >= self.last_ns:
            self.last_ns, self.last = mono, price
        short = self.block['side'] == 'short'
        levels = dict(stop=self.block['stop'], target=self.block['target'],
                      firstTake=self.block['structuralPath'].get('firstTakePrice'))
        for name, level in levels.items():
            if level is None: continue
            crossed = price >= level if (short == (name == 'stop')) else price <= level
            if crossed:
                self.touches[name] = min(self.touches.get(name, mono), mono)

    def result(self, stop):
        complete = stop >= self.end
        out = dict(seconds=self.seconds, windowComplete=complete, printCount=self.count,
            observedMin=self.minimum, observedMax=self.maximum,
            firstTouchSeconds={k: (v-self.block['monoNs'])/1e9 for k,v in self.touches.items()})
        if self.count:
            sign = 1 if self.block['side'] == 'long' else -1
            move = lambda p: sign * (p/self.block['entry']-1) * 10000
            out.update(observedFavorableBps=max(0, move(self.minimum), move(self.maximum)),
                       observedAdverseBps=max(0, -move(self.minimum), -move(self.maximum)),
                       lastObservedMoveBps=move(self.last),
                       lastPrintAgeToBoundarySeconds=(min(stop,self.end)-self.last_ns)/1e9,
                       horizonMoveBps=move(self.last) if complete else None)
        return out


def verify_archive(header, path):
    hashes = header['code']['fileHashes']
    if fingerprint(hashes) != header['code']['sourceSha256']:
        raise ValueError('invalid captured source fingerprint')
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != len(hashes) or set(archive.namelist()) != set(hashes):
            raise ValueError('source archive file set differs')
        for name, expected in hashes.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError('source archive hash differs')


def analyze(directory, feed_name, source_archive):
    root = Path(directory)
    result = json.loads((root/'result.json').read_text(encoding='utf-8'))
    if (root/'failure.json').exists() or result['status'] != 'comparison_completed':
        raise ValueError('capture incomplete')
    ledgers = {n: scan_events(root/f'{n}-events.jsonl') for n in ('baseline', 'candidate')}
    cases, shadow = [], []
    for n, ledger in ledgers.items():
        p = result['portfolios'][n]
        if ledger['closed'] != p['trades'] or not math.isclose(sum(t['netPnl'] for t in ledger['closed']), p['net'], abs_tol=1e-9):
            raise ValueError('closed trade ledger differs from result')
        for block in ledger['blocked'].values():
            cases.append(dict(portfolio=n, **block, windows=[TapeWindow(block,h) for h in HORIZONS]))
        for block in ledger['firstBreaks'].values():
            shadow.append(dict(portfolio=n, **block, windows=[TapeWindow(block,h) for h in HORIZONS]))
    by_symbol = defaultdict(list)
    for case in cases + shadow: by_symbol[case['symbol']].append(case)
    with (root/feed_name).open(encoding='utf-8') as stream:
        header = json.loads(stream.readline())
        if header['schema'] != 'e01-feed-v2': raise ValueError('sealed v2 required')
        verify_archive(header, source_archive)
        for n, veto in (('baseline', False), ('candidate', True)):
            m = result['portfolios'][n]['manifest']
            if (check_manifest(m)[1] or m['config'] != dict(header['config'], e01_breakout_obstacle_veto=veto)
                    or m['code']['sourceSha256'] != header['code']['sourceSha256']
                    or fingerprint(m['runtime']) != fingerprint(header['runtime'])):
                raise ValueError('manifest/header mismatch')
        count, start, stop, previous = 0, None, None, -1
        for row in observations(stream, header):
            count += 1
            mono = row['monoNs']
            if mono < previous: raise ValueError('nonmonotonic inputs')
            previous = mono
            if row['kind'] == 'start':
                if start is not None: raise ValueError('multiple starts')
                start = mono
            if row['kind'] == 'stop':
                if stop is not None: raise ValueError('multiple stops')
                stop = mono
            if row['kind'] != 'market_message' or row['symbol'] not in by_symbol: continue
            body = row['body']
            if not body['topic'].startswith('publicTrade.'): continue
            if body['topic'] != 'publicTrade.' + row['symbol']:
                raise ValueError('tape topic symbol mismatch')
            receipt = body['receipt_mono_ns']
            if receipt > mono: raise ValueError('receipt after application')
            for print_ in body['data']:
                if print_.get('s', row['symbol']) != row['symbol']: raise ValueError('tape symbol mismatch')
                price = float(print_['p'])
                for case in by_symbol[row['symbol']]:
                    for window in case['windows']: window.add(receipt, price)
    with (root/feed_name).open('rb') as stream:
        stream.seek(max(0, (root/feed_name).stat().st_size-512))
        footer = json.loads(stream.read().splitlines()[-1])
    if count != result['eventsConsumed'] or footer['sharedInputHash'] != result['sharedInputHash']:
        raise ValueError('feed/result mismatch')
    if start is None or stop is None: raise ValueError('missing trading window')
    for case in cases + shadow:
        if not start <= case['monoNs'] <= stop: raise ValueError('rejection outside trading window')
        case['windows'] = [w.result(stop) for w in case['windows']]
    candidates = set().union(*(set(l['signals']) for l in ledgers.values()))
    admissions = []
    for key in sorted(candidates):
        states = {n: dict(signalObserved=key in l['signals'], openCount=len(l['opened'].get(key, [])),
                        blockers=l['blocked'].get(key, {}).get('blockers', [])) for n,l in ledgers.items()}
        admissions.append(dict(zip(('symbol','strategy','side','setupId'), key), portfolios=states,
            differingOpen=bool(states['baseline']['openCount']) != bool(states['candidate']['openCount'])))
    breakout = [a for a in admissions if a['strategy'] == 'level_breakout']
    return dict(schema='e01-decision-diagnostics-v1', sourceFeed=feed_name, inputHash=result['sharedInputHash'],
        validatedInputs=count, sourceArchiveVerified=True, currentSourceParityRequired=False,
        fullReplayPerformed=False, profitabilityProven=False,
        uniqueBreakoutCandidates=len(breakout), differingBreakoutOpens=sum(a['differingOpen'] for a in breakout),
        methodology=dict(identity='symbol/strategy/side/setupId (generation); repeated decisions/rearms collapse',
            candidates='Recorded tradeable decisions only; WAIT generations are not candidates',
            admission='Observed trade_opened, not unrecorded order approval or hypothetical fill',
            windows='60/180/300s from first arbiter rejection per setup; publicTrade receipt monotonic time',
            prices='Tape prints versus decision entry; no fills, slippage, fees, dynamic exits or hypothetical PnL',
            coverage='Within recorded union of active symbols only; no claim about all market opportunities',
            waitCounts='Changed decision events, not time spent, independent setups or missed trades'),
        firstBreakMethodology='First recorded WAIT/BREAK per symbol/zoneGeneration after Start, all cases; '
            'reference marketContext.lastPrice, support=short/resistance=long; no entry/stop/target existed. '
            'Gross price diagnostics only, not missed profitable trades. States can overlap and reset.',
        portfolios={n:l['summary'] for n,l in ledgers.items()}, admissions=admissions,
        blockedCases=cases, firstBreakCases=shadow)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory')
    p.add_argument('--feed-name', required=True)
    p.add_argument('--source-archive', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    report = analyze(args.directory, args.feed_name, args.source_archive)
    with Path(args.output).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k:v for k,v in report.items() if k not in ('portfolios','admissions','blockedCases','firstBreakCases','methodology')}))
    for case in report['blockedCases']:
        print(json.dumps({k:v for k,v in case.items() if k != 'structuralPath'}, ensure_ascii=False))


if __name__ == '__main__': main()
