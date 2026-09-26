import hashlib
import json
import runpy

import pytest


analysis = runpy.run_path('scripts/analyze-breakout-entry-evidence.py')


def write_ledger(path, rows):
    raw = b''.join((json.dumps(row) + '\n').encode() for row in rows)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_entry_evidence_is_prefix_causal_and_same_generation(tmp_path):
    path = tmp_path / 'events.jsonl'
    def decision(second, generation, near):
        return {'event': 'decision', 'symbol': 'TEST', 'monoNs': second*10**9,
                'payload': {'strategy': 'level_breakout', 'action': 'wait',
                            'details': {'zoneGeneration': ['support', generation], 'state': 'break',
                                        'pressure': {'nearCloses': near, 'compressedPullbacks': 2}}}}
    details = {'zoneGeneration': ['support', 'g1'], 'pressure': {'nearCloses': 5, 'compressedPullbacks': 2},
               'flow': {'imbalance5s': -.7, 'priceMove5sPct': -.002}}
    rows = [{'event': 'bot_started', 'monoNs': 0, 'payload': {}},
            decision(1, 'g1', 3), decision(9, 'g2', 99),
            {'event': 'trade_opened', 'symbol': 'TEST', 'monoNs': 10*10**9,
             'payload': {'plan': {'strategy': 'level_breakout', 'strategy_details': details,
                                  'setup_id': 'setup', 'notional': 100}}}]
    prefix = analysis['analyze'](path, write_ledger(path, rows))
    rows += [decision(11, 'g1', 999),
             {'event': 'trade_closed', 'monoNs': 12*10**9, 'payload': {'netPnl': 1000000}}]
    full = analysis['analyze'](path, write_ledger(path, rows))
    assert full['entries'] == prefix['entries']
    entry = full['entries'][0]
    assert entry['features']['directionalPriceMove5sBps'] == 20
    assert entry['features']['directionalImbalance5s'] == .7
    assert entry['priorDecisionSamples'][-1]['sample']['features']['nearCloses'] == 3
    assert entry['priorDecisionSamples'][-1]['sampleAgeSeconds'] == 9
    assert entry['priorDecisionSamples'][0]['sample'] is None
    assert len(full['firstBreaks']) == 2


def test_missing_evidence_is_not_false_or_zero():
    features = analysis['evidence']({}, 'long')
    assert features['bothExistingStructuralPoints'] is None
    assert features['directionalPriceMove5sBps'] is None
    assert features['entryFreshness'] is None


def test_tampered_or_out_of_order_ledger_rejected(tmp_path):
    path = tmp_path / 'events.jsonl'
    rows = [{'event': 'bot_started', 'monoNs': 2, 'payload': {}}]
    write_ledger(path, rows)
    with pytest.raises(ValueError, match='SHA-256'):
        analysis['analyze'](path, 'wrong')
    rows.append({'event': 'bot_stopped', 'monoNs': 1, 'payload': {}})
    with pytest.raises(ValueError, match='non-monotonic'):
        analysis['analyze'](path, write_ledger(path, rows))
