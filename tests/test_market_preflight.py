import pytest

from scalp_bot.bybit import MarketMessage
from scalp_bot.preflight import RuntimeMarketProbe


def snapshot(
    topic: str,
    *,
    seq: int,
) -> MarketMessage:
    return MarketMessage(
        topic=topic,
        type="snapshot",
        data={
            "u": 1,
            "seq": seq,
            "b": [["100", "1"]],
            "a": [["101", "1"]],
        },
    )


@pytest.mark.asyncio
async def test_runtime_probe_requires_exact_runtime_stream_set() -> None:
    probe = RuntimeMarketProbe(
        symbol="AAAUSDT",
        fast_depth=50,
        deep_depth=1000,
    )

    await probe.on_message(
        snapshot(
            "orderbook.50.AAAUSDT",
            seq=10,
        )
    )
    assert probe.ready is False

    await probe.on_message(
        snapshot(
            "orderbook.1000.AAAUSDT",
            seq=9,
        )
    )
    assert probe.ready is False

    await probe.on_message(
        MarketMessage(
            topic="kline.1.AAAUSDT",
            data=[{"start": 1}],
        )
    )
    assert probe.ready is False

    await probe.on_message(
        MarketMessage(
            topic="publicTrade.AAAUSDT",
            data=[{
                "T": 1,
                "p": "100",
                "v": "1",
                "S": "Buy",
            }],
        )
    )

    assert probe.ready is True
    health = probe.diagnostics()
    assert health["fastBookSynced"] is True
    assert health["deepBookSynced"] is True
    assert health["sawKline1m"] is True
    assert health["sawPublicTrade"] is True
    assert health["fastSeq"] == 10
    assert health["deepSeq"] == 9


@pytest.mark.asyncio
async def test_runtime_probe_same_depth_uses_one_book_snapshot() -> None:
    probe = RuntimeMarketProbe(
        symbol="AAAUSDT",
        fast_depth=50,
        deep_depth=50,
    )

    await probe.on_message(
        snapshot(
            "orderbook.50.AAAUSDT",
            seq=11,
        )
    )
    await probe.on_message(
        MarketMessage(
            topic="kline.1.AAAUSDT",
            data=[{"start": 1}],
        )
    )
    await probe.on_message(
        MarketMessage(
            topic="publicTrade.AAAUSDT",
            data=[{
                "T": 1,
                "p": "100",
                "v": "1",
                "S": "Sell",
            }],
        )
    )

    assert probe.ready is True
    assert probe.diagnostics()["deepSeq"] == 11
