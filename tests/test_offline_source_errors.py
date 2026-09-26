import json

import pytest

from scalp_bot.offline_bootstrap import restore_cold_engine
from scalp_bot.offline_scheduler import OfflineScheduledReplay
from scalp_bot.offline_segment import SegmentMismatch
from test_offline_context import fixture as context_fixture
from test_offline_transport import fixture as transport_fixture
from test_offline_segment import rehash


@pytest.mark.asyncio
async def test_partial_context_failure_preserves_history_and_next_batch_recovers(tmp_path):
    live, prefix, rows, calls = await context_fixture(tmp_path, fail_then_recover=True)
    replay = restore_cold_engine(prefix)
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    outputs = [r for r in rows if r['event'] != 'replay_input']
    try:
        assert 'PRIVATE-REST-DETAIL' not in json.dumps(rows)
        failures = [r for r in inputs if r['kind'] == 'source_error']
        assert len(failures) == 1 and failures[0]['symbol'] == 'AAA'
        report = await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert report['outputsMatch'] and not report['parityReady']
        for symbol in ('AAA', 'BBB'):
            assert replay.sessions[symbol].candles == live.sessions[symbol].candles
            assert replay.sessions[symbol].context_5m == live.sessions[symbol].context_5m
        assert replay.sessions['AAA'].candles[-1].start_ms == 900000
    finally:
        await live.close(); await replay.close()


@pytest.mark.asyncio
async def test_actual_book_gap_clears_fast_book_and_recovers_from_snapshot(tmp_path, monkeypatch):
    live, prefix, inputs, outputs = await transport_fixture(tmp_path, monkeypatch, gap=True)
    replay = restore_cold_engine(prefix)
    try:
        fault = next(r for r in inputs if r['kind'] == 'transport' and r['body']['phase'] == 'fault')
        assert fault['body']['fastState']['synced'] is False
        assert fault['body']['fastState']['bids'] == []
        assert fault['body']['deepState']['synced'] is True
        assert (await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs))['outputsMatch']
        assert replay.sessions['AAA'].book_synced
        assert replay.sessions['AAA'].orderbook == live.sessions['AAA'].orderbook
    finally:
        await live.close(); await replay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['outcome', 'gap_removed', 'snapshot_state'])
async def test_rehashed_gap_contradictions_are_not_swallowed(tmp_path, monkeypatch, fault):
    live, prefix, inputs, outputs = await transport_fixture(tmp_path, monkeypatch, gap=True)
    replay = restore_cold_engine(prefix)
    if fault == 'outcome':
        next(r for r in inputs if r['kind'] == 'scope' and r['body'].get('outcome') == 'raised')['body']['outcome'] = 'returned'
    elif fault == 'gap_removed':
        next(r for r in inputs if r['kind'] == 'market_message' and r['body']['type'] == 'delta')['body']['data']['u'] = 2
    else:
        next(r for r in inputs if r['kind'] == 'transport' and r['body']['phase'] == 'fault')['body']['fastState']['synced'] = True
    rehash(inputs)
    try:
        with pytest.raises(RuntimeError):
            await OfflineScheduledReplay(replay).apply(inputs, expected_events=outputs)
        assert replay.replay_origin['state'] == 'failed'
    finally:
        await live.close(); await replay.close()
