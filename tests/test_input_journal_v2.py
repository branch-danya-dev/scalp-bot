from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Candle, Candidate
from scalp_bot.engine import TradingEngine
from scalp_bot.execution import FeeSchedule
from scalp_bot.input_journal import InputJournal, validate_input_journal, LEGACY_MISSING_COVERAGE
from scalp_bot.instrument import InstrumentSpec
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.runtime_clock import ReplayRuntimeClock
from test_input_journal import capture, write_report
from test_manifest_validation import example, rehash


def test_v1_journals_remain_readable_with_their_original_coverage(tmp_path):
    journal, _, rows = capture()
    journal.append('callback', None, {'name': 'arbiter'})
    journal.close()
    previous = None
    for row in rows:
        p = row['payload']
        p['schema'] = 'replay-input-v1'
        if p['kind'] == 'header':
            p['body']['missingCoverage'] = list(LEGACY_MISSING_COVERAGE)
        p['previousHash'] = previous
        p['hash'] = fingerprint({k: v for k, v in p.items() if k != 'hash'})
        previous = p['hash']
    report = write_report(tmp_path, rows)
    assert report['structuralStatus'] == 'checks_passed'
    assert report['schema'] == 'replay-input-v1'
    assert 'manifest_binding' in report['missingCoverage']


@pytest.mark.parametrize('fault,expected', [
    (None, None), ('config', 'capture_run_provenance_mismatch'),
    ('wrong_end', 'run_end_manifest_mismatch'), ('unclosed', 'run_not_closed'),
    ('reused', 'overlapping_or_reused_run'),
])
def test_manifest_binding_even_when_all_row_hashes_are_valid(tmp_path, fault, expected):
    journal, _, rows = capture()
    original = example()
    journal.append('manifest', None, {'phase': 'capture', 'manifest': original})
    run = deepcopy(original)
    run['manifestId'] = 'b' * 32
    if fault == 'config':
        run['config']['risk_fraction'] = .012345
        run['configSha256'] = fingerprint(run['config'])
        rehash(run)
    journal.append('manifest', None, {'phase': 'run', 'manifest': run})
    if fault != 'unclosed':
        journal.append('run_end', None, {'manifestId': 'c' * 32 if fault == 'wrong_end' else run['manifestId'],
            'manifestSha256': run['manifestSha256'], 'reason': 'manual'})
    if fault == 'reused':
        journal.append('manifest', None, {'phase': 'run', 'manifest': run})
    journal.close()
    report = write_report(tmp_path, rows)
    assert report['captureManifestChecked']
    if expected:
        assert report['issues'][expected] and report['structuralStatus'] == 'rejected'
    else:
        assert report['structuralStatus'] == 'checks_passed' and report['boundRuns'] == 1
    assert not report['parityReady']


@pytest.mark.asyncio
async def test_bootstrap_rest_and_lifecycle_can_be_reapplied_without_network(tmp_path, monkeypatch):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path / 'capture')), clock=clock, capture_inputs=True)
    replay = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path / 'replay')), clock=clock)
    instrument = InstrumentSpec('AAAUSDT', 'Trading', .01, .1, .1, 5, 1000, 1000, 480, 50)
    fees = FeeSchedule('AAAUSDT', .0002, .00055)
    async def instrument_info(symbol): return instrument
    async def fee_schedule(symbol): return fees
    async def klines(symbol, timeframe, limit):
        return [Candle(900000, 100, 101, 99, 100, 10, 1000)]
    monkeypatch.setattr(engine.rest, 'instrument_info', instrument_info)
    monkeypatch.setattr(engine.rest, 'fee_schedule', fee_schedule)
    monkeypatch.setattr(engine.rest, 'klines', klines)
    try:
        await engine._bootstrap_symbol('AAAUSDT')
        refresh = [Candle(900000, 100, 102, 99, 101, 20, 2000, False)]
        engine._apply_context_result(engine.sessions['AAAUSDT'], (refresh, [], [], []))
        assert refresh[0].confirmed  # Live reconciliation mutates this input.
        rows = [json.loads(line) for line in engine.recorder.path.read_text().splitlines()]
        inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
        boot = next(p['body'] for p in inputs if p['kind'] == 'bootstrap')
        context = next(p['body'] for p in inputs if p['kind'] == 'rest_context')
        assert context['candles'][0]['confirmed'] is False  # Captured before mutation.
        assert boot['candles'][0]['start_ms'] == 900000
        replay._apply_bootstrap_result('AAAUSDT', (InstrumentSpec(**boot['instrument']),
            FeeSchedule(**boot['fees']), *[[Candle(**c) for c in boot[k]]
                for k in ('candles', 'context5m', 'context15m', 'context1h')]))
        replay._apply_context_result(replay.sessions['AAAUSDT'], tuple(
            None if context[k] is None else [Candle(**c) for c in context[k]]
            for k in ('candles', 'context5m', 'context15m', 'context1h')))
        a, b = engine.sessions['AAAUSDT'], replay.sessions['AAAUSDT']
        assert a.candles == b.candles and a.instrument == b.instrument and a.fee_schedule == b.fee_schedule
        assert a.market_snapshot() == b.market_snapshot()
        engine._deactivate_symbol('AAAUSDT', 'test')
    finally:
        await engine.close()
        await replay.close()
    report = validate_input_journal(engine.recorder.path)
    assert report['structuralStatus'] == 'checks_passed'
    assert report['counts']['bootstrap'] == report['counts']['rest_context'] == 1
    assert report['counts']['symbol_lifecycle'] == 2


@pytest.mark.asyncio
async def test_scanner_order_failures_and_actual_run_binding(tmp_path, monkeypatch):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path), working_symbols=1), capture_inputs=True)
    candidates = [Candidate('ZZZUSDT', 2e6, .01, 100), Candidate('AAAUSDT', 1e6, .02, 50)]
    async def active(): return candidates
    async def skip(*args): pass
    monkeypatch.setattr(engine.rest, 'active_candidates', active)
    monkeypatch.setattr(engine, '_promote_symbol', skip)
    monkeypatch.setattr(engine, '_cleanup_active_symbols', skip)
    monkeypatch.setattr(engine, 'start_block_reason', lambda: None)
    try:
        await engine._scan_once()
        engine._record_scanner_error('scanner_error', RuntimeError('SENSITIVE ERROR TEXT'))
        engine.set_running(True)
        engine.set_running(False)
    finally:
        await engine.close()
    report = validate_input_journal(engine.recorder.path)
    assert report['structuralStatus'] == 'checks_passed' and report['boundRuns'] == 1
    inputs = [json.loads(x)['payload'] for x in engine.recorder.path.read_text().splitlines()
              if json.loads(x)['event'] == 'replay_input']
    scanner = next(p['body'] for p in inputs if p['kind'] == 'scanner_result')
    assert [c['symbol'] for c in scanner['candidates']] == ['ZZZUSDT', 'AAAUSDT']
    assert 'SENSITIVE ERROR TEXT' not in json.dumps(inputs)


def test_v2_rejects_unclassified_metadata_and_unverified_manifest():
    journal, _, _ = capture()
    candidate = asdict(Candidate('AAAUSDT', 1, 0, 1))
    candidate['api_key'] = 'secret'
    with pytest.raises(ValueError):
        journal.append('scanner_result', None, {'candidates': [candidate]})
    with pytest.raises(ValueError):
        journal.append('manifest', None, {'phase': 'capture', 'manifest': {'manifestId': 'a' * 32}})
