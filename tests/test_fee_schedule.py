import asyncio

import pytest

from scalp_bot.bybit import BybitRestClient
from scalp_bot.config import Settings
from scalp_bot.execution import FeeSchedule, fee_rate


def test_fee_rate_prefers_per_symbol_schedule() -> None:
    cfg = Settings(
        maker_fee_rate=0.00020,
        taker_fee_rate=0.00055,
    )
    schedule = FeeSchedule(
        symbol="BTCUSDT",
        maker_fee_rate=0.00010,
        taker_fee_rate=0.00032,
        source="bybit_account",
    )

    assert fee_rate(
        cfg,
        "maker_limit",
        schedule,
    ) == pytest.approx(0.00010)
    assert fee_rate(
        cfg,
        "taker_market",
        schedule,
    ) == pytest.approx(0.00032)


@pytest.mark.asyncio
async def test_fee_schedule_falls_back_without_private_credentials() -> None:
    cfg = Settings(
        fee_rate_mode="account_if_available",
        bybit_api_key="",
        bybit_api_secret="",
        maker_fee_rate=0.00020,
        taker_fee_rate=0.00055,
    )
    client = BybitRestClient(cfg)
    try:
        schedule = await client.fee_schedule(
            "BTCUSDT"
        )
    finally:
        await client.close()

    assert schedule.source == "configured_no_credentials"
    assert schedule.maker_fee_rate == pytest.approx(0.00020)
    assert schedule.taker_fee_rate == pytest.approx(0.00055)


@pytest.mark.asyncio
async def test_fee_schedule_uses_account_endpoint_when_available(
    monkeypatch,
) -> None:
    cfg = Settings(
        fee_rate_mode="account_required",
        bybit_api_key="public-key",
        bybit_api_secret="private-secret",
    )
    client = BybitRestClient(cfg)

    async def fake_private_get(path, params):
        assert path == "/v5/account/fee-rate"
        assert params == {
            "category": "linear",
            "symbol": "ETHUSDT",
        }
        return {
            "list": [{
                "symbol": "ETHUSDT",
                "makerFeeRate": "0.0001",
                "takerFeeRate": "0.0003",
            }]
        }

    monkeypatch.setattr(
        client,
        "_private_get",
        fake_private_get,
    )
    try:
        schedule = await client.fee_schedule(
            "ETHUSDT"
        )
    finally:
        await client.close()

    assert schedule.source == "bybit_account"
    assert schedule.maker_fee_rate == pytest.approx(0.0001)
    assert schedule.taker_fee_rate == pytest.approx(0.0003)
