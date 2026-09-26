from time import perf_counter_ns, time

import pytest

from scalp_bot.bybit import BybitError, BybitRestClient, MarketMessage
from scalp_bot.config import Settings
from scalp_bot.domain import Candle, TradeTick
from scalp_bot.engine import ActiveSymbolSession
from test_engine_lifecycle import make_engine, book, plan


def synchronize(engine, offset_ms=1000):
    mono = perf_counter_ns() / 1e9
    wall = time() * 1000
    assert engine.market_clock.synchronize(server_ms=wall + offset_ms,
        sent_mono=mono - 0.01, received_mono=mono, received_wall_ms=wall)
    return wall + offset_ms, mono


def session(engine):
    s = ActiveSymbolSession(symbol="AAAUSDT", orderbook=book(), deep_orderbook=book(),
                            book_synced=True, deep_book_synced=True, last_price=100)
    s.fast_receipt_mono = s.deep_receipt_mono = s.trade_receipt_mono = perf_counter_ns() / 1e9
    s.last_market_at = s.last_book_at = s.last_deep_book_at = time()
    reading = engine._clock_state()
    if reading and reading["valid"]:
        s.fast_book_exchange_ts_ms = s.deep_book_exchange_ts_ms = s.trade_exchange_ts_ms = reading["evaluationMs"] - 1
    engine.sessions[s.symbol] = s
    return s


@pytest.mark.asyncio
async def test_features_include_received_trades_ahead_of_local_wall(tmp_path):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        exchange_ms, _ = synchronize(engine, offset_ms=10_000)
        s = session(engine)
        start = int(exchange_ms // 60_000) * 60_000
        s.candles = [Candle(start-60_000, 100, 101, 99, 100, 10, 1000),
                     Candle(start, 100, 101, 99, 100, 10, 1000, confirmed=False)]
        s.trades.append(TradeTick(int(exchange_ms-10), 100, 2, "Buy", sequence=1))
        s.latest_processed_event_ms = int(exchange_ms-10)
        await engine._evaluate(s)
        assert s.market_context.observed_at_ms > int(time() * 1000) + 9000
        assert s.flow_context is not None
        frame = s.frame(5, None)
        assert frame["tradeFlow"]["tradeCount5s"] == 1
        assert frame["clock"]["valid"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_invalid_clock_blocks_entry_but_hard_stop_still_executes(tmp_path):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        s = session(engine)
        assert engine._clock_entry_block(s) == "clock:unsynchronized"
        assert "clock" in engine.start_block_reason()
        engine._arbitrate_once()
        assert not engine.broker.positions
        engine.broker.open(plan(s.symbol), book())
        s.orderbook = s.deep_orderbook = book(99, 99.02)
        s.last_price = 99
        engine._mark_execution_from_market(s, trade_ts_ms=int(time()*1000), trade_price=99)
        assert not engine.broker.positions
        assert engine.broker.closed_trades[-1]["reason"] == "stop"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_independent_receipt_ages_cannot_be_refreshed_by_other_streams(tmp_path):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        synchronize(engine)
        s = session(engine)
        assert engine._clock_entry_block(s) is None
        s.deep_receipt_mono -= 10
        assert engine._clock_entry_block(s) == "clock:stale_book_receipt"
        s.deep_receipt_mono = perf_counter_ns() / 1e9
        s.trade_receipt_mono -= 10
        assert engine._clock_entry_block(s) == "clock:stale_trade_receipt"
        s.trade_receipt_mono = perf_counter_ns() / 1e9
        s.fast_receipt_mono = None
        assert engine._clock_entry_block(s) == "clock:stale_book_receipt"
        s.fast_receipt_mono = perf_counter_ns() / 1e9
        s.fast_book_exchange_ts_ms -= 20_000
        assert engine._clock_entry_block(s) == "clock:stale_fast_book_event"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_trade_batch_exposes_only_processed_prefix_with_original_receipt(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        exchange, _ = synchronize(engine)
        s = session(engine)
        seen = []
        monkeypatch.setattr(engine, "_mark_execution_from_market",
                            lambda current, **kw: seen.append([t.ts_ms for t in current.trades]))
        receipt_ns = perf_counter_ns() - 10_000_000_000
        message = MarketMessage(topic="publicTrade.AAAUSDT", receipt_mono_ns=receipt_ns,
            data=[{"T": int(exchange), "p": "100", "v": "1", "S": "Buy"},
                  {"T": int(exchange)+1, "p": "101", "v": "1", "S": "Buy"}])
        await engine._process_public_trade_message(s, message)
        assert seen == [[int(exchange)], [int(exchange), int(exchange)+1]]
        assert s.trade_receipt_mono == receipt_ns / 1e9
        assert engine._clock_entry_block(s) == "clock:stale_trade_receipt"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_sync_error_and_expiry_fail_closed(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        async def fail():
            raise BybitError("unavailable")
        monkeypatch.setattr(engine.rest, "clock_sample", fail)
        await engine._sync_clock_once()
        assert not engine._clock_state()["valid"]
        assert engine.events[0]["event"] == "clock_sync_error"
        synchronize(engine)
        engine.market_clock.max_sync_age_seconds = 0
        assert engine._clock_state()["reason"] == "synchronization_expired"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_clock_loss_cancels_resting_entry_before_it_can_fill(tmp_path):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        s = session(engine)
        p = plan(s.symbol)
        p.entry_mode = "maker_limit"
        engine.broker.place_pending(p)
        assert s.symbol in engine.broker.pending_entries
        engine._mark_execution_from_market(s, trade_ts_ms=int(time()*1000), trade_price=99,
                                           trade_notional_usd=100_000, trade_side="Sell")
        assert not engine.broker.pending_entries
        assert not engine.broker.positions
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_bybit_clock_sample_uses_server_nanoseconds_and_request_interval(monkeypatch):
    client = BybitRestClient(Settings(_env_file=None))
    try:
        async def get(path, params, *, timing):
            assert path == "/v5/market/time" and params == {}
            timing.update(sent_mono=10.0, received_mono=10.1, received_wall_ms=1790000000223)
            return {"timeNano": "1790000000123456789"}
        monkeypatch.setattr(client, "_get", get)
        sample = await client.clock_sample()
        assert sample["server_ms"] == pytest.approx(1790000000123.4568)
        assert sample["received_mono"] >= sample["sent_mono"]
        async def invalid(*args, **kwargs):
            return {"timeSecond": "1790000000"}
        monkeypatch.setattr(client, "_get", invalid)
        with pytest.raises(BybitError, match="timeNano"):
            await client.clock_sample()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wall_jump_does_not_expire_pending_or_cut_position_early(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        pos = engine.broker.open(plan("AAAUSDT"), book())
        p = plan("BBBUSDT")
        p.entry_mode = "maker_limit"
        pending = engine.broker.place_pending(p)
        assert pending is not None
        wall = time()
        monkeypatch.setattr(engine.clock, "time", lambda: wall + 100_000)
        assert not engine.broker.expire_pending()
        assert not engine.broker._should_cut_no_follow_through(pos, -2)
        pending.created_mono -= 100
        assert engine.broker.expire_pending()[0]["reason"] == "passive_entry_timeout"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_http_clock_timing_excludes_pacing_and_failed_attempts(monkeypatch):
    import httpx
    virtual = [100.0]
    attempts = []
    async def handler(request):
        attempts.append(request)
        virtual[0] += .05
        if len(attempts) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"retCode": 0, "result": {"timeNano": "1790000000000000000"}})
    client = BybitRestClient(Settings(_env_file=None, rest_rate_limit_retries=1))
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async def pace():
        virtual[0] += 5.0
    monkeypatch.setattr(client, "_pace_request", pace)
    monkeypatch.setattr(client, "_retry_delay", lambda *args: 0)
    monkeypatch.setattr("scalp_bot.bybit.perf_counter_ns", lambda: int(virtual[0] * 1e9))
    try:
        sample = await client.clock_sample()
        assert len(attempts) == 2
        assert sample["sent_mono"] == pytest.approx(110.05)
        assert sample["received_mono"] - sample["sent_mono"] == pytest.approx(.05)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_market_health_exposes_real_clock_failure(tmp_path):
    engine = make_engine(tmp_path, exchange_clock_enabled=True)
    try:
        s = session(engine)
        health = engine.market_health()
        assert not health["ready"]
        assert health["reason"] == "exchange clock is not ready: unsynchronized"
        assert health["clockBlocks"][s.symbol] == "clock:unsynchronized"
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_valid,expected", [
    (False, [2.0, 2.0, 20.0]), (True, [20.0, 2.0, 20.0]),
])
async def test_clock_loop_retries_rejection_then_resumes_normal_interval(tmp_path, monkeypatch,
                                                                         initial_valid, expected):
    engine = make_engine(tmp_path, exchange_clock_enabled=True, clock_sync_interval_seconds=20)
    delays = []
    results = iter([False, True])
    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            engine._stop.set()
    async def sync():
        return next(results)
    monkeypatch.setattr(engine, "_clock_state", lambda: {"valid": initial_valid})
    monkeypatch.setattr(engine, "_sync_clock_once", sync)
    monkeypatch.setattr("scalp_bot.engine.asyncio.sleep", sleep)
    try:
        await engine._clock_loop()
        assert delays == expected
    finally:
        await engine.close()
