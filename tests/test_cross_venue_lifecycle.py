import asyncio
from types import SimpleNamespace

from scalp_bot.cross_venue_public import PublicCrossVenueService


async def test_watch_removed_task_is_joined_before_service_close():
    service = PublicCrossVenueService(SimpleNamespace(), SimpleNamespace())
    service.specs = {"okx": {"BTCUSDT": {}}, "binance": {"BTCUSDT": {}}}
    started, released, finished = asyncio.Event(), asyncio.Event(), []
    async def stream(venue, symbol):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await released.wait()
            finished.append((venue, symbol))
    service._stream = stream
    service.watch(["BTCUSDT"])
    await started.wait()
    await asyncio.sleep(0)
    service.watch([])
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    try:
        assert not closing.done(), "close returned while cancelled external tasks still owned live work"
    finally:
        released.set()
        await closing
        await asyncio.sleep(0)
    assert sorted(finished) == [("binance", "BTCUSDT"), ("okx", "BTCUSDT")]
