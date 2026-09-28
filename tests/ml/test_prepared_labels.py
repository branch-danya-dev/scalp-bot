from dataclasses import asdict, replace
from copy import deepcopy
import pytest
from scalp_bot.config import Settings
from scalp_bot.domain import Side, TradeTick, TradePlan, OrderBook
from scalp_bot.ml.prepared_labels import PreparedPath, LabelPolicy
from scalp_bot.paper import PaperBroker


def book(bid, ask):
    return OrderBook([(bid, 100)], [(ask, 100)])


def plan(symbol, side):
    return TradePlan(symbol, "level_breakout", side, 100, 100, 99.5, 101, 400, .4,
        2, 4, 0, 4, 2, 2, 0, "setup")


def prepared(maker=False):
    p = plan("AAAUSDT", Side.LONG)
    p.strategy = "level_breakout"
    if maker:
        p.entry_mode = "maker_limit"
    return dict(row=dict(identity="i", source=dict(available_mono_ns=1_000_000_000,
        source_sequence=10, capture_id="c"), available_wall_ms=100000,
        intent=dict(episode_key="e"), features=[1, None]), frozenPlan=asdict(p),
        segment={}, crossVenue={}, crossVenueAlignment="unavailable", epoch=1)


def advance(path, ms, **kwargs):
    b = book(99.99, 100.)
    args = dict(now_ns=1_000_000_000+ms*1_000_000, wall_ms=100000+ms,
        sequence=10+ms, epoch=1, book=b, depth=b, fresh=True, exchange_ms=100000+ms)
    args.update(kwargs)
    return path.advance(**args)


def test_labels_use_frozen_plan_and_same_broker_stop_costs():
    cfg = Settings(_env_file=None)
    raw = prepared()
    path = PreparedPath(raw, cfg, LabelPolicy())
    raw["frozenPlan"]["target"] = 999
    assert path.plan.target == 101
    assert advance(path, 249) is None and path.entered_ns is None
    advance(path, 250)
    assert path.entered_ns == 1_250_000_000
    control = PaperBroker(cfg, clock=path.clock)
    control.open(deepcopy(path.plan), book(99.99, 100))
    b = book(99.4, 99.41)
    result = advance(path, 300, book=b, depth=b)
    events = control.mark("AAAUSDT", b.mid, b, depth_book=b, trade_price=None, observed_at_ms=100300)
    expected = next(e for e in events if e["event"] == "trade_closed")
    assert result["netPnl"] == expected["netPnl"]
    assert result["stop"] and result["target_before_stop"] is False
    assert result["label_end_wall_ms"] == 100300 and result["portfolioPnl"] is None


@pytest.mark.parametrize("overrides,reason", [({"fresh":False}, "depth_gap"),
    ({"epoch":2}, "transport_epoch"), ({"wall_ms":999000}, "local_wall_clock_step")])
def test_gaps_are_censored_not_zero(overrides, reason):
    path = PreparedPath(prepared(), Settings(_env_file=None), LabelPolicy())
    result = advance(path, 250, **overrides)
    assert result["censor_reason"] == reason
    assert result["realized_net_r"] is None and not result["trainingReady"]


def test_maker_same_received_batch_cannot_fill_and_right_censor():
    path = PreparedPath(prepared(maker=True), Settings(_env_file=None), LabelPolicy())
    advance(path, 250, sequence=50)
    tick = TradeTick(100260, 99.9, 1000, "Sell", sequence=1)
    advance(path, 260, sequence=50, tick=tick)
    assert path.entered_ns is None
    advance(path, 270, sequence=51, tick=tick)
    assert path.entered_ns == 1_270_000_000
    result = advance(path, 180001)
    assert result["censor_reason"] == "right_censored_horizon"


def test_maker_entry_batch_cannot_also_fill_its_maker_exit():
    path = PreparedPath(prepared(maker=True), Settings(_env_file=None), LabelPolicy())
    advance(path, 250, sequence=50)
    tick = TradeTick(100260, 99.9, 1000, "Sell", sequence=1)
    advance(path, 270, sequence=51, tick=tick)
    up = book(100.6, 100.61)
    exit_tick = TradeTick(100290, 100.7, 1000, "Buy", sequence=2)
    advance(path, 300, sequence=51, tick=exit_tick, book=up, depth=up)
    assert not path.broker.positions["AAAUSDT"].partial_taken
