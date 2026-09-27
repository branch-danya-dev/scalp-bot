import pytest
from scalp_bot.config import Settings
from scalp_bot.domain import OrderBook
from scalp_bot.maker_shadow import MakerShadowEngine


def setup():
    rows = []
    engine = MakerShadowEngine(Settings(_env_file=None), lambda k, v: rows.append((k, v)))
    book = OrderBook([(100, 2)], [(100.02, 2)])
    candidate = engine.post("AAA", "long", epoch=1, now_ns=1, sequence=1, exchange_ms=1000,
        book=book, quantity=1, depth_fresh=True)
    return engine, candidate, rows, book


def trade(engine, **kw):
    values = dict(epoch=1, now_ns=10_000_000, sequence=2, tick_sequence=1,
        exchange_ms=1010, price=99.9, quantity=2.5, aggressor="Sell", depth_fresh=True)
    values.update(kw)
    engine.trade("AAA", **values)


@pytest.mark.parametrize("change", [{"price":100}, {"aggressor":"Buy"}, {"aggressor":""},
    {"sequence":1}, {"exchange_ms":1000}, {"quantity":0}])
def test_no_touch_wrong_side_same_batch_stale_or_missing_volume_fills(change):
    engine, candidate, rows, book = setup()
    trade(engine, **change)
    assert candidate.filled == 0


def test_queue_depletion_partial_fills_and_executable_markout():
    engine, c, rows, book = setup()
    trade(engine)
    assert c.filled == .5 and c.queue_remaining == 0
    trade(engine, tick_sequence=2, quantity=.5, now_ns=20_000_000)
    assert c.filled == 1 and not engine.active
    for ms in (120, 520, 1020):
        engine.book("AAA", epoch=1, now_ns=ms*1_000_000, book=book, depth_fresh=True)
    assert not engine.observing
    outcome = next(v for k, v in rows if k == "maker_outcome")
    assert outcome["filled"] and outcome["portfolioPnl"] is None
    assert outcome["lots"][0]["markouts"]["100"]["netAfterCosts"] < 0


@pytest.mark.parametrize("reason", ["cancel", "reconnect", "depth"])
def test_cancel_before_fill_reconnect_and_missing_depth(reason):
    engine, c, rows, book = setup()
    if reason == "cancel":
        engine.cancel(c, "requote", 2)
        trade(engine)
    elif reason == "reconnect":
        trade(engine, epoch=2)
    else:
        trade(engine, depth_fresh=False)
    assert c.filled == 0 and not engine.active
