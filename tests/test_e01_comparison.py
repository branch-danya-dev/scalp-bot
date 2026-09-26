from copy import deepcopy
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from scalp_bot.config import Settings
from scalp_bot.domain import Action
from scalp_bot.e01_comparison import E01Comparison
from scalp_bot.e01_comparison import _copy_feed_value
from scalp_bot.manifest_schema import PUBLIC_CONFIG_FIELDS
from scalp_bot.strategy.semantic_arbiter import assess_candidate, assess_session_candidates
from test_semantic_arbiter import context, mature_level, decision
from test_offline_portfolio import fixture


def test_feed_copy_preserves_values_aliases_cycles_and_isolation():
    nested = {'p': ['0.001', 1.25, None, True, 2**70]}
    original = {'a': nested, 'b': nested, 'fallback': {1, 2}}
    copied = _copy_feed_value(original)
    assert copied == deepcopy(original)
    assert copied['a'] is copied['b'] and copied['a'] is not nested
    copied['a']['p'].append('changed')
    assert original['a']['p'][-1] == 2**70
    cycle = []
    cycle.append(cycle)
    clone = _copy_feed_value(cycle)
    assert clone is clone[0] and clone is not cycle


@pytest.mark.asyncio
async def test_engine_passes_current_flow_to_real_breakout_without_recomputing(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('strategy must reuse the engine evaluation flow')
    monkeypatch.setattr('scalp_bot.strategy.breakout.compute_trade_flow', forbidden)
    engine, _, _ = await fixture(tmp_path, monkeypatch, production=True)
    assert len(engine.broker.closed_trades) == 1


@pytest.mark.parametrize('side', [Action.LONG, Action.SHORT])
@pytest.mark.parametrize('own', [False, True])
def test_e01_veto_only_foreign_breakout_obstacle(side, own):
    long = side == Action.LONG
    level = mature_level('resistance' if long else 'support',
        100.05 if long else 99.95, 100.05 if long else 99.95, generation='obstacle')
    ctx = context(**{'resistance' if long else 'support': level})
    trade = decision('level_breakout', side, entry=100, stop=99.5 if long else 100.5,
        target=101 if long else 99, watched_level=100, details=dict(state='impulse',
            levelLifecycle={'generation_id': 'obstacle' if own else 'other'},
            opportunityFreshness={'classification': 'fresh'},
            flowAlignment={'classification': 'strongly_aligned'}))
    baseline = assess_candidate(trade, ctx)
    candidate = assess_candidate(trade, ctx, breakout_obstacle_veto=True)
    assert baseline.allowed
    assert candidate.allowed == own
    assert ('e01_foreign_obstacle_before_first_take' in candidate.blockers) == (not own)
    if own:
        assert candidate == baseline
    assert assess_session_candidates([trade], ctx, breakout_obstacle_veto=True)['level_breakout'].allowed == own


def observations(rows):
    """Test-only projection of this known synthetic fixture, not a log converter."""
    inputs = [r['payload'] for r in rows if r['event'] == 'replay_input']
    boots = {r['symbol']: r['body'] for r in inputs if r['kind'] == 'bootstrap'}
    scopes = {}
    current = None
    for row in inputs:
        kind, body, symbol = row['kind'], row['body'], row['symbol']
        if kind == 'service' and body['phase'] == 'close': break
        if kind == 'scope':
            if body['phase'] == 'begin':
                scopes[body['id']] = body['parentId']
                current = body['id']
            else:
                current = scopes[body['id']]
            continue
        if kind == 'scanner_result': kind, body = 'scanner', dict(candidates=body['candidates'], bootstrap=boots)
        elif kind == 'control' and body == {'name': 'set_running', 'value': True}: kind, body = 'start', {}
        elif kind == 'callback' and body['name'] in ('evaluate', 'arbiter') and scopes.get(current) is None:
            kind, body = body['name'], {}
        elif kind not in ('market_message', 'clock_sample'): continue
        yield dict(kind=kind, symbol=symbol, body=deepcopy(body),
            wallSeconds=row['processingWallSeconds'], monoNs=row['processingMonoNs'])


@pytest.mark.asyncio
async def test_pair_uses_real_strategy_independent_state_and_bound_manifests(tmp_path, monkeypatch):
    live, _, rows = await fixture(tmp_path, monkeypatch, production=True)
    pair = E01Comparison(live.config)
    for observation in observations(rows):
        await pair.apply(observation)
    report = pair.finish()
    assert report['netDifference'] == 0  # No foreign obstacle before first take in this fixture.
    assert report['profitabilityProven'] is False
    a, b = pair.engines.values()
    assert a.broker is not b.broker and a.strategies['level_breakout'] is not b.strategies['level_breakout']
    assert a.sessions['AAA'] is not b.sessions['AAA']
    assert len(a.broker.closed_trades) == len(b.broker.closed_trades) == 1
    assert a.broker.closed_trades == b.broker.closed_trades
    assert a._run_manifest['config']['e01_breakout_obstacle_veto'] is False
    assert b._run_manifest['config']['e01_breakout_obstacle_veto'] is True
    differing = {k for k in a._run_manifest['config'] if a._run_manifest['config'][k] != b._run_manifest['config'][k]}
    assert differing == {'e01_breakout_obstacle_veto'}
    a.broker.balance -= 1
    assert a.broker.balance != b.broker.balance


@pytest.mark.asyncio
async def test_pair_fails_closed_on_unsupported_input():
    pair = E01Comparison(Settings(_env_file=None, event_driven_evaluation_enabled=False))
    with pytest.raises(ValueError):
        await pair.apply(dict(kind='unknown', symbol=None, body={}, wallSeconds=1, monoNs=1))
    with pytest.raises(ValueError): pair.finish()


@pytest.mark.asyncio
async def test_legacy_foreign_obstacle_flag_cannot_add_second_context_veto(tmp_path, monkeypatch):
    live, _, rows = await fixture(tmp_path, monkeypatch, production=True)
    pair = E01Comparison(live.config)
    feed = list(observations(rows))
    assert feed[-1]['kind'] == 'arbiter'
    for row in feed[:-1]:
        await pair.apply(row)
    # Controlled candidate/context for this admission unit, independent of the
    # unmodified-strategy integration test above. Positions are never inserted.
    for engine in pair.engines.values():
        session = engine.sessions['AAA']
        session.market_context = context(resistance=mature_level('resistance', 100.45, 100.45, generation='other'))
        session.decisions = {'level_breakout': decision('level_breakout', Action.LONG,
            entry=100.365, stop=99.9, target=101.5, watched_level=100.24,
            details=dict(state='impulse', levelLifecycle={'generation_id': 'own'},
                opportunityFreshness={'classification': 'fresh'},
                flowAlignment={'classification': 'strongly_aligned'}))}
    await pair.apply(feed[-1])
    a, b = pair.engines.values()
    assert 'AAA' in a.broker.positions and 'AAA' in b.broker.positions
    assert a.broker.positions['AAA'] is not b.broker.positions['AAA']
    assert not any('e01_foreign_obstacle_before_first_take' in r['payload'].get('blockers', [])
                   for r in b.recorder.rows if r['event'] == 'arbiter_blocked')
    report = pair.finish()
    assert len(report['portfolios']['baseline']['trades']) == 1
    assert len(report['portfolios']['candidate']['trades']) == 1
    assert report['portfolios']['candidate']['balance'] == report['portfolios']['baseline']['balance']


def test_e01_does_not_change_other_playbooks():
    trade = decision('weak_level_rejection', Action.LONG, entry=100, stop=99, target=101)
    assert assess_candidate(trade, context()) == assess_candidate(trade, context(), breakout_obstacle_veto=True)


@pytest.mark.asyncio
async def test_normalized_feed_file_produces_serializable_report(tmp_path, monkeypatch):
    live, _, rows = await fixture(tmp_path, monkeypatch, production=True)
    path = tmp_path / 'feed.jsonl'
    header = dict(schema='e01-feed-v1', config={k: getattr(live.config, k) for k in PUBLIC_CONFIG_FIELDS})
    path.write_text('\n'.join(json.dumps(r) for r in [header, *observations(rows)])+'\n', encoding='utf-8')
    runner = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts' / 'compare-e01.py'))
    result = await runner['run'](path, 100_000)
    assert result['status'] == 'comparison_completed'
    assert json.loads(json.dumps(result))['netDifference'] == 0
    with pytest.raises(ValueError, match='event limit'):
        await runner['run'](path, 1)


def test_report_ledger_survives_broker_200_trade_cache():
    trades = [dict(fees=i/100, netPnl=-i/100) for i in range(205)]
    engine = SimpleNamespace(broker=SimpleNamespace(closed_trades=trades[-200:], total_closed_trades=205),
        recorder=SimpleNamespace(rows=[dict(event='trade_closed', payload=t) for t in trades]))
    assert E01Comparison._trade_ledger(engine) == trades
    engine.recorder.rows.pop(0)
    with pytest.raises(ValueError, match='incomplete comparison trade ledger'):
        E01Comparison._trade_ledger(engine)
