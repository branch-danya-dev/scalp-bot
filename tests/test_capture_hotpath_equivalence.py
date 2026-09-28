from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import random
import struct

import pytest

from scalp_bot.manifest_validation import fingerprint
from scalp_bot.parallel_scenarios import ParallelScenarioRouter
from scalp_bot.scenario import Scenario
from scalp_bot.input_journal import InputJournal, detach_json
from scalp_bot.runtime_clock import ReplayRuntimeClock


def test_reused_canonical_encoder_matches_stdlib_across_threads():
    values = [dict(sequence=i, body={'ключ': ['Привет', -0.0, 1e-12, 1e22, i/7, None]},
        previousHash='a'*64) for i in range(300)]
    expected = [hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest() for v in values]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(fingerprint, values)) == expected
    for number in (float('nan'), float('inf'), -float('inf')):
        with pytest.raises(ValueError):
            fingerprint({'value': number})


def test_context_scenario_snapshot_is_fully_detached_and_fresh():
    @dataclass
    class Context:
        scenario: dict | None = None
    router = ParallelScenarioRouter()
    s = Scenario('AAA', 'id', 'level_breakout', 'long', 'gen', 100, 1, 1, 1, 300, ['reason'],
        level={'nested': [1]}, preview={'nested': [2]}, last_rejection={'nested': [3]},
        frozen={'budget': 2, 'entryArea': [99, 101]})
    router.children[s.owner].scenarios[s.symbol] = s
    first = router.context_for(Context(), s.symbol, s.owner)
    first.scenario['level']['nested'].append(10)
    first.scenario['preparation']['nested'].append(20)
    first.scenario['lastRejection']['nested'].append(30)
    first.scenario['entryArea'].append(40)
    first.scenario['reasons'].append('changed')
    first.scenario['times']['observed'] = -1
    fresh = router.context_for(Context(), s.symbol, s.owner).scenario
    assert fresh['level'] == {'nested': [1]}
    assert fresh['preparation'] == {'nested': [2]}
    assert fresh['lastRejection'] == {'nested': [3]}
    assert fresh['entryArea'] == [99, 101]
    assert fresh['reasons'] == ['reason'] and fresh['times']['observed'] == 1


def test_scalar_clock_encoding_matches_canonical_bytes_and_memory_charge():
    rows = []
    class Sink:
        defer_journal_hashes = True
        def record(self, event, symbol, row):
            rows.append(row)
    clock = ReplayRuntimeClock(wall_seconds=-0.0, mono_ns=0)
    journal = InputJournal(Sink().record, clock)
    rng = random.Random(20260928)
    values = [-0.0, 0.0, 1e-300, -1e300, 1e-7, 1e16, 1790549621.9885652, 10**100]
    for _ in range(1000):
        value = struct.unpack('d', rng.randbytes(8))[0]
        if math.isfinite(value):
            values.append(value)
    for index, value in enumerate(values):
        method = 'perf_counter_ns' if type(value) is int else ('time' if index % 2 else 'monotonic')
        body = dict(scopeId=index if index % 3 else None, method=method, value=value)
        journal.append('clock_read', None, body)
        assert rows[-1].retained_bytes == detach_json(body)[1] + 2048
        body['method'] = 'changed after enqueue'
    journal.close()
    previous = None
    for deferred in rows:
        expected = dict(deferred.row, previousHash=previous)
        digest = fingerprint(expected)
        actual = deferred.resolve()
        assert actual == dict(expected, hash=digest)
        previous = digest


@pytest.mark.parametrize('method,value', [('time', float('nan')), ('monotonic', float('inf')),
    ('perf_counter_ns', -1), ('time', []), ('"quoted"', 1)])
def test_scalar_clock_fast_path_does_not_bypass_validation(method, value):
    journal = InputJournal(lambda *args: None, ReplayRuntimeClock(wall_seconds=1, mono_ns=1))
    with pytest.raises(ValueError):
        journal.append('clock_read', None, dict(scopeId=None, method=method, value=value))
