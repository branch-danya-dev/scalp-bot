from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from scalp_bot.ml.engine_dataset import EngineDatasetCollector
from scalp_bot.ml.features import FEATURE_NAMES,extract_context_features,ContextCoverage
from scalp_bot.runtime_clock import ReplayRuntimeClock
from scalp_bot.domain import OrderBook
from test_features import context
from test_dataset_learning import instrument


def prepared(tmp_path):
    collector=EngineDatasetCollector(tmp_path/'dataset','unique-capture')
    ctx=context();clock=ReplayRuntimeClock(wall_seconds=ctx.observed_at_ms/1000,mono_ns=100_000_000_000)
    session=SimpleNamespace(symbol=ctx.symbol,market_context=ctx,instrument=instrument(),orderbook=OrderBook([(100,1000)],[(100.01,1000)]),
        book_synced=True,deep_book_is_fresh=lambda now:True)
    engine=SimpleNamespace(clock=clock,recorder=SimpleNamespace(sequence=99),_clock_state=lambda s:None,sessions={ctx.symbol:session})
    collector.starts[ctx.symbol]=0
    return collector,engine,session


def test_real_context_snapshot_has_structure_liquidity_and_no_future_alias(tmp_path):
    c,e,s=prepared(tmp_path);c.sample(e,s)
    rows=[deepcopy(p.row) for p in c.pending[s.symbol]]
    assert len(rows)==2
    for name in ('structure_known','liquidity_known'):
        assert rows[0]['features'][FEATURE_NAMES.index(name)]==1
    assert rows[0]['features'][FEATURE_NAMES.index('support_distance_bps')]==100
    s.market_context.liquidity.remaining_ratio=123
    s.market_context=replace(s.market_context,structure=replace(s.market_context.structure,support_distance_pct=1000))
    assert [p.row for p in c.pending[s.symbol]]==rows


def test_stale_context_and_unknown_instrument_cannot_mint_training_row(tmp_path):
    c,e,s=prepared(tmp_path);s.market_context=replace(s.market_context,observed_at_ms=s.market_context.observed_at_ms-1);c.sample(e,s)
    assert not c.pending
    s.market_context=replace(s.market_context,observed_at_ms=s.market_context.observed_at_ms+1);s.instrument=None;c.sample(e,s)
    assert not c.pending and c.excluded['instrument_unavailable']==1


def test_unobserved_quote_interval_censors_labels_and_restarts_warmup(tmp_path):
    c,e,s=prepared(tmp_path);c.sample(e,s);c.quote(e,s.symbol)
    assert len(c.pending[s.symbol])==2
    e.clock.set_observation(wall_seconds=122,mono_ns=102_000_000_000)
    c.quote(e,s.symbol)
    assert not c.pending[s.symbol] and c.epochs[s.symbol]==1
    assert c.excluded['transport_gap']==2
