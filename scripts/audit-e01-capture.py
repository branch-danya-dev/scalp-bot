"""Stream-check a completed E01 capture and reconcile its financial ledger.

Does not replay strategies or establish profitability. Run as a script, like
the collector, so importlib.metadata sees the same entry-point search path.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path

import msgspec

from scalp_bot.e01_feed import read_header, observations
from scalp_bot.manifest_validation import check_manifest, fingerprint


def audit(directory, *, feed_name='feed.jsonl'):
    root = Path(directory)
    result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    if (root / 'failure.json').exists() or result.get('status') != 'comparison_completed':
        raise ValueError('capture is failed or incomplete')
    experiment = result.get('experiment', 'E01')
    if experiment not in ('E01','E06'): raise ValueError('unsupported experiment')
    treatment_field = 'e06_conditional_breakout_hold' if experiment == 'E06' else 'e01_breakout_obstacle_veto'
    report = dict(status='capture_and_ledger_checks_passed', fullReplayPerformed=False,
                  profitabilityProven=False, experiment=experiment, auditedFeed=feed_name, portfolios={})
    configs = []
    for name in ('baseline', 'candidate'):
        portfolio = result['portfolios'][name]
        if check_manifest(portfolio['manifest'])[1]:
            raise ValueError(f'invalid {name} manifest')
        configs.append(portfolio['manifest']['config'])
        counts, clock = Counter(), Counter()
        trades, rtts = [], []
        start = stop = summary = None
        with (root / f'{name}-events.jsonl').open('rb') as stream:
            for line in stream:
                row = msgspec.json.decode(line)
                event, body = row['event'], row['payload']
                counts[event] += 1
                if event == 'bot_started': start = row['wallSeconds']
                if event == 'bot_stopped': stop = row['wallSeconds']
                if event == 'run_summary': summary = body
                if event == 'trade_closed': trades.append(body)
                if event == 'clock_sync':
                    clock['accepted' if body['accepted'] else str(body['rejectionReason'])] += 1
                    sample = body['sample']
                    rtts.append((sample['received_mono'] - sample['sent_mono']) * 1000)
        if any(counts[event] != 1 for event in ('bot_started', 'bot_stopped', 'run_summary')):
            raise ValueError(f'incomplete {name} lifecycle')
        if trades != portfolio['trades']:
            raise ValueError(f'{name} report/ledger mismatch')
        for actual, expected in ((sum(t['netPnl'] for t in trades), portfolio['net']),
                (sum(t['fees'] for t in trades), portfolio['fees']),
                (portfolio['balance'] - configs[-1]['start_balance'], portfolio['net'])):
            if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9):
                raise ValueError(f'{name} financial reconciliation failed')
        report['portfolios'][name] = dict(balance=portfolio['balance'], net=portfolio['net'],
            fees=portfolio['fees'], closedTrades=len(trades), start=start, stop=stop,
            durationSeconds=stop-start, events=dict(counts), clockSamples=dict(clock),
            rttMedianMs=sorted(rtts)[len(rtts)//2] if rtts else None,
            sampledMaxDrawdownUsd=portfolio['sampledMaxDrawdownUsd'], summary=summary,
            trades=[{k: t.get(k) for k in ('symbol', 'strategy', 'side', 'netPnl', 'fees',
                                         'reason', 'openedAt', 'closedAt')} for t in trades])
    differences = [k for k in configs[0] if configs[0][k] != configs[1][k]]
    if (differences != [treatment_field]
            or configs[0][treatment_field] is not False
            or configs[1][treatment_field] is not True):
        raise ValueError('portfolio configurations differ beyond '+experiment)
    if experiment == 'E06' and any(c['e01_breakout_obstacle_veto'] or c['breakout_hold_without_retest_seconds'] != 8 for c in configs):
        raise ValueError('E06 mixed with another treatment')
    report['configDifferences'] = differences
    health, durations = Counter(), Counter()
    previous = final = None
    with (root / 'equity.jsonl').open('rb') as stream:
        for line in stream:
            row = msgspec.json.decode(line)
            final = row
            if row['wallSeconds'] < report['portfolios']['baseline']['start']:
                continue
            reason = str(row['portfolios']['baseline']['marketHealth']['clock']['reason'])
            health[reason] += 1
            if previous is not None:
                durations[previous[1]] += row['wallSeconds'] - previous[0]
            previous = row['wallSeconds'], reason
    if final is None or any(v['openPositions'] for v in final['portfolios'].values()):
        raise ValueError('missing final equity or unsettled positions')
    report['health'] = dict(samples=dict(health), sampledSecondsByClockReason=dict(durations))
    report['finalEquity'] = {n: {k: v[k] for k in ('balance', 'equity', 'openPositions')}
                            for n, v in final['portfolios'].items()}
    for name, values in report['finalEquity'].items():
        if not math.isclose(values['balance'], report['portfolios'][name]['balance'], abs_tol=1e-9):
            raise ValueError('final equity/report balance mismatch')
    phases = Counter()
    with (root / 'transport.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            phases[row['phase']] += 1
            if row['phase'] in ('fault', 'backpressure_detail'):
                raise ValueError('transport failure in completed capture')
    report['transport'] = dict(phases)
    with (root / feed_name).open(encoding='utf-8') as stream:
        header = read_header(stream)  # Keep the original source/runtime guards.
        if header['schema'] not in ('e01-feed-v2','paired-feed-v3') or header.get('experiment','E01') != experiment:
            raise ValueError('live audit requires a sealed feed with matching experiment')
        for name, treatment in (('baseline', False), ('candidate', True)):
            manifest = result['portfolios'][name]['manifest']
            if (manifest['config'] != dict(header['config'], **{treatment_field:treatment})
                    or manifest['code']['sourceSha256'] != header['code']['sourceSha256']
                    or fingerprint(manifest['runtime']) != fingerprint(header['runtime'])):
                raise ValueError('portfolio manifest differs from capture header')
        kinds, histogram = Counter(), Counter()
        count = 0
        max_lag, max_depth = 0.0, 0
        for row in observations(stream, header):
            count += 1
            kinds[row['kind']] += 1
            if row['kind'] == 'market_message':
                body = row['body']
                lag = body['queue_lag_ms']
                histogram[int(lag)] += 1
                max_lag = max(max_lag, lag)
                max_depth = max(max_depth, body['queue_depth'])
        if count != result['eventsConsumed']:
            raise ValueError('feed/report count mismatch')
        if kinds['start'] != 1 or kinds['stop'] != 1:
            raise ValueError('expected one shared trading window')
    with (root / feed_name).open('rb') as stream:
        stream.seek(max(0, (root / feed_name).stat().st_size - 512))
        footer = json.loads(stream.read().splitlines()[-1])
    if footer['sharedInputHash'] != result['sharedInputHash']:
        raise ValueError('feed/report hash mismatch')
    quantiles = {}
    for q in (.5, .95, .99):
        cumulative = 0
        for ms, frequency in sorted(histogram.items()):
            cumulative += frequency
            if cumulative >= sum(histogram.values()) * q:
                quantiles[str(q)] = ms + 1
                break
    report['input'] = dict(validatedCount=count, kinds=dict(kinds), hash=footer['sharedInputHash'],
        sealed=True, sourceAndRuntimeMatch=True, maxQueueLagMs=max_lag, maxQueueDepth=max_depth,
        queueLagQuantileUpperBoundsMs=quantiles)
    delta = report['portfolios']['candidate']['net'] - report['portfolios']['baseline']['net']
    if not math.isclose(delta, result['netDifference'], abs_tol=1e-9):
        raise ValueError('net difference mismatch')
    report['netDifference'] = delta
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--output', required=True)
    parser.add_argument('--feed-name', default='feed.jsonl', help='Explicit derived feed name, if auditing a documented recovery')
    args = parser.parse_args()
    report = audit(args.directory, feed_name=args.feed_name)
    with Path(args.output).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != 'portfolios'}, ensure_ascii=False))
    for name, values in report['portfolios'].items():
        print(name, json.dumps({k: v for k, v in values.items() if k not in ('summary', 'trades', 'events')}))


if __name__ == '__main__':
    main()
