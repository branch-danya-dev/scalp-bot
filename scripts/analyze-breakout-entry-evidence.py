"""Read-only, causal entry evidence from a hash-bound recorded portfolio.

No strategy execution or hypothetical PnL. Samples are emitted decisions, not
uniform observations; missing values and sample lag remain explicit.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


def evidence(details, side):
    sign = 1 if side == 'long' else -1
    flow = details.get('flow') or {}
    pressure = details.get('pressure') or {}
    forming = pressure.get('formingCandle') or {}
    near = pressure.get('nearCloses')
    compressed = pressure.get('compressedPullbacks')
    structural = None if near is None or compressed is None else near >= 2 and compressed >= 2
    def directional(key, multiplier=1):
        value = flow.get(key)
        return None if value is None else sign * value * multiplier
    return {
        'side': side, 'state': details.get('state'),
        'nearCloses': near, 'compressedPullbacks': compressed,
        'bothExistingStructuralPoints': structural,
        'pressureScore': details.get('pressureScore'),
        'formingPressure': pressure.get('formingPressure'),
        'volumeActive': pressure.get('volumeActive'),
        'flowClassification': (details.get('flowAlignment') or {}).get('classification'),
        'localRegime': (details.get('playbookContext') or {}).get('localRegime'),
        'directionalImbalance5s': directional('imbalance5s'),
        'directionalPriceMove5sBps': directional('priceMove5sPct', 10000),
        'directionalPriceMove15sBps': directional('priceMove15sPct', 10000),
        'directionalPriceMove60sBps': directional('priceMove60sPct', 10000),
        'priceResponseEfficiency5s': flow.get('priceResponseEfficiency5s'),
        'acceleration': flow.get('acceleration'),
        'tradeRateRatio': flow.get('tradeRateRatio'),
        'volumePaceRatio': forming.get('volumePaceRatio'),
        'microRange5sBps': forming.get('microRange5sBps'),
        'directionalResponseBps': details.get('directionalResponseBps'),
        'breakHoldSeconds': details.get('breakHoldSeconds'),
        'retestSeen': details.get('retestSeen'),
        'armToFireSeconds': details.get('armToFireSeconds'),
        'opportunityArm': details.get('opportunityArm'),
        'entryFreshness': details.get('entryFreshness'),
        'stopDistancePct': details.get('stopDistancePct'),
        'setupQuality': details.get('setupQuality'),
    }


def analyze(path, expected_sha256):
    digest = hashlib.sha256()
    histories = defaultdict(list)
    first_break = {}
    ready = {}
    entries = []
    started = False
    previous_mono = -1
    for line_number, raw in enumerate(path.open('rb'), 1):
        digest.update(raw)
        row = json.loads(raw)
        mono = row['monoNs']
        if mono < previous_mono:
            raise ValueError('non-monotonic ledger')
        previous_mono = mono
        if row['event'] == 'bot_started':
            started = True
        if not started:
            continue
        payload = row['payload']
        if row['event'] == 'decision':
            if payload.get('strategy') != 'level_breakout':
                continue
            details = payload.get('details') or {}
        elif row['event'] == 'trade_opened':
            plan = payload['plan']
            if plan['strategy'] != 'level_breakout':
                continue
            details = plan['strategy_details']
        else:
            continue
        generation = details.get('zoneGeneration')
        if not generation or len(generation) != 2:
            continue
        key = (row['symbol'], *generation)
        side = 'long' if generation[0] == 'resistance' else 'short'
        sample = {'symbol': row['symbol'], 'generation': generation,
                  'monoNs': mono, 'line': line_number, 'features': evidence(details, side)}
        if row['event'] == 'decision':
            sample['action'] = payload['action']
            sample['reasons'] = payload.get('reasons', [])
            histories[key].append(sample)
            if details.get('state') == 'break':
                first_break.setdefault(key, sample)
            if payload['action'] in ('long', 'short'):
                ready.setdefault(key, sample)
        else:
            sample['setupId'] = plan['setup_id']
            sample['riskScale'] = details.get('riskScale')
            sample['notional'] = plan['notional']
            sample['priorDecisionSamples'] = []
            for offset in (30, 15, 5, 1):
                cutoff = mono - offset * 1_000_000_000
                prior = next((s for s in reversed(histories[key]) if s['monoNs'] <= cutoff), None)
                sample['priorDecisionSamples'].append({
                    'offsetSeconds': offset,
                    'sampleAgeSeconds': None if prior is None else (mono-prior['monoNs'])/1e9,
                    'sample': prior,
                })
            first = first_break.get(key)
            sample['secondsSinceFirstEmittedBreak'] = None if first is None else (mono-first['monoNs'])/1e9
            entries.append(sample)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ValueError('ledger SHA-256 mismatch')
    return {'schemaVersion': 1, 'ledgerSha256': actual, 'holdout': False,
            'method': 'entry features use trade_opened plan only; history uses preceding same-generation decisions; no exit/future fields',
            'limitations': 'change-based snapshots, not all evaluations; first BREAK is not preparation age; no hypothetical portfolio PnL',
            'firstBreaks': list(first_break.values()), 'firstReady': list(ready.values()), 'entries': entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', type=Path)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.ledger, args.expected_sha256)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    print(json.dumps({k: len(result[k]) for k in ('firstBreaks', 'firstReady', 'entries')}))


if __name__ == '__main__':
    main()
