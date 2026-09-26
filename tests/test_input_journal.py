from copy import deepcopy
import json

import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.config import Settings
from scalp_bot.engine import ActiveSymbolSession, TradingEngine
from scalp_bot.input_journal import EVENT, InputJournal, validate_input_journal
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.runtime_clock import ReplayRuntimeClock


def capture():
    rows = []
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    journal = InputJournal(lambda event, symbol, payload: rows.append(
        {'event': event, 'symbol': symbol, 'payload': payload}), clock)
    return journal, clock, rows


def message():
    return MarketMessage(topic='publicTrade.AAAUSDT', ts=1_000_000,
        data=[{'T': 1_000_000, 'p': '100', 'v': '2', 'S': 'Buy'}],
        receipt_wall_ns=999_000_000_000, receipt_mono_ns=9_000_000_000,
        parsed_mono_ns=9_100_000_000, processor_started_mono_ns=9_200_000_000,
        event_id='market-1', otel_span=object())


def write_report(tmp_path, rows):
    path = tmp_path / 'input.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf8')
    return validate_input_journal(path)


def test_capture_preserves_full_batch_and_receipt_without_mutable_aliases(tmp_path):
    journal, clock, rows = capture()
    market = message()
    journal.market_message('AAAUSDT', market)
    market.data[0]['p'] = '200'
    clock.set_observation(wall_seconds=900, mono_ns=11_000_000_000)
    journal.append('callback', 'AAAUSDT', {'name': 'evaluate'})
    journal.close()
    journal.close()
    body = rows[1]['payload']['body']
    assert body['data'][0]['p'] == '100'
    assert body['receipt_mono_ns'] == 9_000_000_000
    assert 'otel_span' not in body
    assert rows[1]['payload']['processingMonoNs'] == 10_000_000_000
    report = write_report(tmp_path, rows)
    assert report['structuralStatus'] == 'checks_passed'
    assert not report['parityReady'] and report['missingCoverage']
    assert report['counts']['market_message'] == 1
    with pytest.raises(RuntimeError):
        journal.append('callback', None, {'name': 'arbiter'})


@pytest.mark.parametrize('change,issue', [
    ('drop', 'sequence_gap_or_reorder'), ('reorder', 'sequence_gap_or_reorder'),
    ('edit', 'content_hash_mismatch'), ('duplicate', 'sequence_gap_or_reorder'),
    ('clock', 'processing_clock_reorder'), ('footer', 'footer_count_mismatch'),
    ('after_footer', 'input_after_footer'), ('future_receipt', 'receipt_after_processing'),
])
def test_corruption_is_not_a_usable_input_stream(tmp_path, change, issue):
    journal, clock, rows = capture()
    journal.market_message('AAAUSDT', message())
    journal.append('callback', None, {'name': 'arbiter'})
    journal.close()
    if change == 'drop': rows.pop(1)
    elif change == 'reorder': rows[1], rows[2] = rows[2], rows[1]
    elif change == 'edit': rows[1]['payload']['body']['data'][0]['p'] = '90'
    elif change == 'duplicate': rows.insert(2, deepcopy(rows[1]))
    elif change == 'clock': rows[2]['payload']['processingMonoNs'] = 1
    elif change == 'footer': rows[-1]['payload']['body']['inputCount'] = 99
    elif change == 'after_footer': rows.append(deepcopy(rows[1]))
    elif change == 'future_receipt': rows[1]['payload']['body']['receipt_mono_ns'] = 20_000_000_000
    report = write_report(tmp_path, rows)
    assert report['structuralStatus'] == 'rejected'
    assert report['issues'][issue] > 0
    assert not report['parityReady']


def test_missing_tail_or_absent_capture_is_incomplete(tmp_path):
    journal, _, rows = capture()
    journal.append('callback', None, {'name': 'arbiter'})
    assert write_report(tmp_path, rows)['structuralStatus'] == 'incomplete'
    report = write_report(tmp_path, [{'event': 'research_frame', 'payload': {}}])
    assert report['coverage'] == 'not_recorded'
    assert report['structuralStatus'] == 'incomplete'


def test_unknown_receipt_is_not_invented(tmp_path):
    journal, _, rows = capture()
    market = message()
    market.receipt_mono_ns = 0
    journal.market_message('AAAUSDT', market)
    journal.close()
    assert write_report(tmp_path, rows)['missingReceiptCount'] == 1


def test_invalid_kind_and_extra_transport_fields_rejected_before_sequence_change():
    journal, _, _ = capture()
    for kind, body in [('auth', {'token': 'DO_NOT_RECORD'}),
                       ('callback', {'name': 'arbiter', 'headers': {}})]:
        with pytest.raises(ValueError):
            journal.append(kind, None, body)
        assert journal.sequence == 1


def test_validator_bounds_memory_and_detects_incomplete_json(tmp_path):
    path = tmp_path / 'large.jsonl'
    path.write_bytes(b'x' * 5000 + b'\n{"event":')
    report = validate_input_journal(path, max_row_bytes=128)
    assert report['issues'] == {'row_too_large': 1, 'partial_row': 1}
    assert report['source']['bytesRead'] == path.stat().st_size


def test_rehashed_partial_coverage_cannot_claim_parity(tmp_path):
    journal, _, rows = capture()
    journal.close()
    header = rows[0]['payload']
    header['body']['parityReady'] = True
    header['hash'] = fingerprint({k: v for k, v in header.items() if k != 'hash'})
    report = write_report(tmp_path, rows)
    assert report['issues']['invalid_body'] == 1 and not report['parityReady']


@pytest.mark.asyncio
async def test_engine_captures_processing_boundary_clock_and_callbacks(tmp_path, monkeypatch):
    clock = ReplayRuntimeClock(wall_seconds=1000, mono_ns=10_000_000_000)
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path),
        exchange_clock_enabled=True), clock=clock, capture_inputs=True)
    engine.sessions['AAAUSDT'] = ActiveSymbolSession(symbol='AAAUSDT', clock=clock)
    market = message()
    async def stream(url, symbol, on_message, stop, **kwargs):
        await on_message(market)
    async def sample():
        return dict(server_ms=1_000_000, sent_mono=9.99, received_mono=10, received_wall_ms=1_000_000)
    monkeypatch.setattr('scalp_bot.engine.stream_symbol', stream)
    monkeypatch.setattr(engine.rest, 'clock_sample', sample)
    # No network or background scheduler is started by this test.
    monkeypatch.setattr(engine, '_schedule_event_evaluation', lambda *args, **kwargs: None)
    try:
        await engine._sync_clock_once()
        await engine._symbol_worker('AAAUSDT', engine._stop)
        engine._arbitrate_once()
        engine.set_running(False)
        assert engine.sessions['AAAUSDT'].trades[0].price == 100
    finally:
        await engine.close()
    report = validate_input_journal(engine.recorder.path)
    assert report['structuralStatus'] == 'checks_passed'
    assert report['counts']['clock_sample'] == 1
    assert report['counts']['market_message'] == 1
    assert report['counts']['callback'] >= 2
    assert report['counts']['control'] >= 1
    rows = [json.loads(x)['payload'] for x in engine.recorder.path.read_text().splitlines()
            if json.loads(x)['event'] == EVENT]
    kinds = [x['kind'] for x in rows]
    assert kinds.index('clock_sample') < kinds.index('market_message') < kinds.index('callback')


@pytest.mark.asyncio
async def test_capture_is_disabled_by_default(tmp_path):
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path)))
    try:
        assert engine.input_journal is None
        engine._arbitrate_once()
    finally:
        await engine.close()
    assert not engine.recorder.path.exists()
