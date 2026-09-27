"""2026-09-26 current-12h: causal confirmation and immutable ready phase."""
from dataclasses import replace

import pytest

from scalp_bot.domain import Action, OrderBook, StrategyDecision, Trend
from scalp_bot.scenario import ScenarioRouter
from scalp_bot.strategy.weak_level_rejection import WeakLevelRejectionStrategy
from test_strategies import weak_support_rejection_candles, rejection_absorption_only_flow
from test_scenario_remediation import market, level, ready, SYMBOL
from test_price_action_hypothesis import scenario as seed_context


def recorded_xrp_decision(*, episode_changed):
    """Replay the recorded entry evaluation, with only its prior watch state seeded.

    The 15-second tape and observed context are original, not a cold portfolio.
    Confirmed chart history is the captured 240-bar suffix; it suffices for this
    pinned-zone micro-confirmation test and is not used to rebuild level discovery.
    """
    import importlib.util
    import json
    from pathlib import Path
    from scalp_bot.domain import TradeTick
    from scalp_bot.strategy.common import LevelZone
    from scalp_bot.strategy.weak_level_rejection import RejectionWatchState

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('archived_snapshot', root/'scripts/check-scenario-episodes.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    case = json.loads((root/'tests/fixtures/two_hour_xrp_episode.json').read_text(encoding='utf-8'))
    m = case['market']
    context, candles, structure, book, _ = helper.snapshot(m)
    context = replace(context, scenario=m['marketContext']['scenario'])
    zone = LevelZone(**case['zone'])
    state = RejectionWatchState(zone_key=case['generation'], pinned_zone=zone,
        pinned_generation_id=case['generation'], swept=True,
        armed_at=case['old']['arm']['observedAtMs']/1000,
        armed_price=case['old']['arm']['price'], absorption_at=case['old']['absorption'],
        absorption_price=case['old']['absorption_price'])
    if hasattr(state, 'confirmation_episode'):
        state.confirmation_episode = (case['old']['episode'] if episode_changed
            else context.scenario['episodeKey'])
    strategy = WeakLevelRejectionStrategy()
    strategy._states['XRPUSDT'] = state
    ticks = [TradeTick(int(t), float(p), float(q), side) for t,p,q,side in case['trades']]
    decision = strategy._decision_for_zone(candles, book, context.legacy_trend, ticks,
        'XRPUSDT', zone, structure=structure, generation_id_override=case['generation'],
        observed_at_ms=context.observed_at_ms, market_context=context, trade_flow=m['tradeFlow'])
    return decision, case, state


def test_recorded_xrp_new_sweep_rejects_stale_confirmation():
    decision, _, state = recorded_xrp_decision(episode_changed=True)
    assert not decision.tradeable
    assert state.absorption_at == 0  # Current tape has no new absorption.
    assert not decision.details['microResponseReady']


def test_recorded_xrp_same_episode_retains_earned_confirmation():
    decision, case, _ = recorded_xrp_decision(episode_changed=False)
    assert decision.tradeable
    assert decision.details['microPriceResponseBps'] == pytest.approx(case['expected']['quote_response_bps'])
    assert decision.details['postAbsorptionTapeResponseBps'] == pytest.approx(case['expected']['tape_response_bps'])
    assert decision.details['postAbsorptionTapeTradeCount'] == case['expected']['post_absorption_count']


@pytest.mark.parametrize("direction", [1, -1])
def test_new_rejection_episode_cannot_reuse_previous_absorption(direction):
    strategy = WeakLevelRejectionStrategy()
    rows = weak_support_rejection_candles()
    ticks = rejection_absorption_only_flow()
    observed = ticks[-1].ts_ms
    if direction < 0:
        rows = [replace(c, open=200-c.open, high=200-c.low, low=200-c.high, close=200-c.close) for c in rows]
        ticks = [replace(t, price=200-t.price, side="Buy" if t.side == "Sell" else "Sell") for t in ticks]
    context = seed_context(direction)[2]
    owner = dict(owner=strategy.key, side="long" if direction > 0 else "short",
                 episodeKey='level:failed:quote:20000000:1', state="PREPARED")
    def evaluate(mid, at, episode):
        mid = mid if direction > 0 else 200-mid
        ctx = replace(context, scenario={**owner, "episodeKey":episode},
                      observed_at_ms=at, last_price=mid, forming_candle=None)
        return strategy.evaluate(rows, OrderBook([(mid-.005,50)],[(mid+.005,50)]),
            Trend.UP if direction > 0 else Trend.DOWN, symbol=SYMBOL, trades=ticks,
            market_context=ctx, observed_at_ms=at)
    first = evaluate(100.095, observed, owner["episodeKey"])
    assert not first.tradeable and first.details["attackAbsorbed"]
    original_arm = first.details["opportunityArm"]
    # New sweep/reclaim of the same object. Old quote displacement would FIRE
    # immediately on baseline, even though this episode has no response yet.
    new_episode = 'level:failed:quote:20002200:2'
    changed = evaluate(100.12, observed+1000, new_episode)
    assert not changed.tradeable
    assert changed.details["absorptionAgeSeconds"] == 0
    assert changed.details["opportunityArm"] == original_arm
    # A real response to this episode is still sufficient, without a new hold.
    confirmed = evaluate(100.145, observed+2000, new_episode)
    assert confirmed.tradeable
    assert confirmed.details["microResponseReady"]


def test_ready_contract_does_not_reenter_preparation_after_risk_reject():
    obj = level()
    rows, book, context, structure = market([obj])
    router = ScenarioRouter()
    s = router.observe(SYMBOL,context,rows,structure,{"level_breakout":True},10)
    d = router.accept_decision(SYMBOL,ready(s,book,obj),11,book)
    frozen = dict(s.frozen)
    router.reject(SYMBOL,"risk","recorded economic refusal",12)
    wait = StrategyDecision(s.owner,Action.WAIT,["no current response"],details={"state":"armed"})
    decision = router.accept_decision(SYMBOL,wait,13,book)
    assert not decision.tradeable  # Phase retention must not manufacture an order.
    assert s.state == "ARMED"
    assert s.frozen == frozen and s.first_signal_mono == 11
    repeated = router.accept_decision(SYMBOL,d,14,book)
    assert repeated.tradeable and s.first_signal_mono == 11


@pytest.mark.asyncio
async def test_slow_websocket_close_drains_before_engine_shutdown_deadline(tmp_path, monkeypatch):
    """A peer that doesn't acknowledge close must not outlive the service budget."""
    import asyncio
    import inspect
    from scalp_bot import bybit
    from scalp_bot.config import Settings
    from scalp_bot.engine import TradingEngine

    received = asyncio.Event()
    events = []
    default_close = inspect.signature(bybit.websockets.connect).parameters['close_timeout'].default

    class Socket:
        async def send(self, *args, **kwargs):
            pass

        async def recv(self, **kwargs):
            received.set()
            await asyncio.Event().wait()

    class SlowClose:
        def __init__(self, seconds):
            self.seconds = seconds

        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            # Model the installed transport's bounded wait for peer close.
            await asyncio.sleep(self.seconds)
            return False

    monkeypatch.setattr(bybit.websockets, 'connect',
        lambda *a, **kw: SlowClose(kw.get('close_timeout', default_close)))
    engine = TradingEngine(Settings(_env_file=None, session_dir=str(tmp_path)))
    async def callback(message):
        pytest.fail('No market data is provided by this offline fixture')
    task = asyncio.create_task(bybit._stream_topics('wss://example.invalid',
        ['orderbook.50.AAA'], callback, engine._stop, on_transport=events.append))
    engine._tasks.append(task)
    try:
        await received.wait()
        await engine._shutdown_service_tasks()
        assert task.done()
        assert [e['phase'] for e in events][-2:] == ['cancelled', 'drained']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        engine._tasks.clear()
        await engine.close()


@pytest.mark.parametrize('index', range(6), ids=[f'T{i:02}' for i in range(1,7)])
def test_recorded_partial_and_protective_exits_reconcile(index):
    """Actual accepted positions/book snapshots, not six re-created admissions.

    In particular preserve the profitable T01 partial + stop and ETH's original
    protective exit. This local control does not invent counterfactual PnL.
    """
    import json
    from pathlib import Path
    from dataclasses import fields
    from scalp_bot.config import Settings
    from scalp_bot.domain import Side
    from scalp_bot.paper import PaperBroker, Position
    from scalp_bot.research import _book_from_public
    from scalp_bot.runtime_clock import ReplayRuntimeClock

    folder = Path(__file__).resolve().parents[1]/'docs/run-reviews/two-hour-20260926'
    case = json.loads((folder/'execution_controls.json').read_text(encoding='utf-8'))[index]
    config = Settings(_env_file=None, **json.loads((folder/'captured_config.json').read_text(encoding='utf-8')))
    data = dict(case['open']); data['side'] = Side(data['side'])
    pos = Position(**{f.name:data[f.name] for f in fields(Position) if f.name in data})
    clock = ReplayRuntimeClock(wall_seconds=pos.opened_at, mono_ns=int(pos.opened_mono*1e9))
    broker = PaperBroker(config, clock=clock)
    broker.positions[pos.symbol] = pos
    initial_stop, target = pos.stop, pos.target
    fast = _book_from_public(case['close_books']['fastOrderbook'])
    deep = _book_from_public(case['close_books']['deepOrderbook'])
    for partial in case['partials']:
        got = broker._partial_take(pos, fast)
        for key in ('closedQuantity','fill','netPnl','newStop','newTarget'):
            assert got[key] == pytest.approx(partial[key])
    if not case['partials']:
        assert pos.stop == initial_stop
    assert pos.target == target
    # A tape print alone cannot override executable-side stop detection.
    results = broker.mark(pos.symbol, fast.mid, fast, depth_book=deep, trade_price=None)
    closed = next(e for e in results if e['event']=='trade_closed')
    assert closed['reason'] == 'stop'
    for key in ('exit','grossPnl','fees','fundingPnlUsd','netPnl'):
        assert closed[key] == pytest.approx(case['close'][key])
    if index == 0:
        assert closed['netPnl'] > 0
