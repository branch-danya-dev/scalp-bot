import pytest

from scalp_bot.bybit import BybitRestClient
from scalp_bot.config import Settings
from scalp_bot.activity import activity_score, correlation_1h
from scalp_bot.domain import Candidate, Candle


def candle(i: int, close: float) -> Candle:
    return Candle(i * 60_000, close, close, close, close, 1, close)


def test_one_hour_correlation_uses_aligned_returns() -> None:
    btc = [candle(i, 100 + i) for i in range(61)]
    coin = [candle(i, 50 + i * 0.5) for i in range(61)]
    corr = correlation_1h(coin, btc)
    assert corr is not None
    assert corr > 0.99


def test_activity_score_rewards_large_move_and_recent_burst() -> None:
    quiet = Candidate(
        "QUIETUSDT",
        turnover_24h=200_000_000,
        change_24h=0.01,
        last_price=1,
        activity_change=0.001,
        activity_turnover=500_000,
        correlation_1h_btc=0.9,
    )
    hot = Candidate(
        "HOTUSDT",
        turnover_24h=600_000_000,
        change_24h=0.96,
        last_price=1,
        activity_change=0.02,
        activity_turnover=8_000_000,
        correlation_1h_btc=-0.37,
    )
    assert activity_score(hot, 5) > activity_score(quiet, 5)


def test_activity_score_prefers_local_relative_turnover_burst() -> None:
    normal = Candidate(
        "NORMALUSDT",
        turnover_24h=500_000_000,
        change_24h=0.03,
        last_price=1,
        activity_change=0.005,
        activity_turnover=2_000_000,
        activity_burst_ratio=1.0,
    )
    burst = Candidate(
        "BURSTUSDT",
        turnover_24h=500_000_000,
        change_24h=0.03,
        last_price=1,
        activity_change=0.005,
        activity_turnover=2_000_000,
        activity_burst_ratio=3.0,
    )
    assert activity_score(burst, 5) > activity_score(normal, 5)



@pytest.mark.asyncio
async def test_liquid_candidates_filter_unexecutable_spread() -> None:
    client = BybitRestClient(
        Settings(
            min_turnover_usd=100_000_000,
            max_entry_drift_bps=8,
        )
    )

    async def fake_get(path: str, params: dict) -> dict:
        assert path == "/v5/market/tickers"
        return {
            "list": [
                {
                    "symbol": "TIGHTUSDT",
                    "turnover24h": "500000000",
                    "price24hPcnt": "0.01",
                    "lastPrice": "100",
                    "volume24h": "1000000",
                    "bid1Price": "99.96",
                    "ask1Price": "100.04",
                    "bid1Size": "20",
                    "ask1Size": "18",
                },
                {
                    "symbol": "WIDEUSDT",
                    "turnover24h": "600000000",
                    "price24hPcnt": "0.03",
                    "lastPrice": "100",
                    "volume24h": "1000000",
                    "bid1Price": "99.90",
                    "ask1Price": "100.10",
                    "bid1Size": "100",
                    "ask1Size": "100",
                },
            ]
        }

    client._get = fake_get  # type: ignore[method-assign]
    try:
        rows = await client.liquid_candidates()
        assert [row.symbol for row in rows] == ["TIGHTUSDT"]
        assert rows[0].spread_bps == pytest.approx(8.0)
        assert rows[0].top_book_notional_usd > 0
    finally:
        await client.close()



@pytest.mark.asyncio
async def test_rest_client_falls_back_to_bytick_after_403() -> None:
    cfg = Settings(
        bybit_rest_url="https://api.bybit.com",
        bybit_rest_fallback_urls="https://api.bytick.com",
        rest_rate_limit_retries=0,
    )
    client = BybitRestClient(cfg)
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "api.bybit.com":
            return __import__("httpx").Response(
                403,
                text="Forbidden",
                request=request,
            )
        return __import__("httpx").Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "result": {"list": []},
            },
            request=request,
        )

    await client.client.aclose()
    import httpx
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=10.0,
    )
    try:
        result = await client._get(
            "/v5/market/tickers",
            {"category": "linear"},
        )
        assert result == {"list": []}
        assert client.active_rest_url == "https://api.bytick.com"
        assert any("api.bybit.com" in url for url in calls)
        assert any("api.bytick.com" in url for url in calls)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rest_client_reports_all_global_403_endpoints() -> None:
    cfg = Settings(
        bybit_rest_url="https://api.bybit.com",
        bybit_rest_fallback_urls="https://api.bytick.com",
        rest_rate_limit_retries=0,
    )
    client = BybitRestClient(cfg)

    def handler(request):
        import httpx
        return httpx.Response(
            403,
            text="region blocked",
            request=request,
        )

    await client.client.aclose()
    import httpx
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=10.0,
    )
    try:
        with pytest.raises(
            Exception,
            match="Bybit Global REST is unavailable",
        ) as exc:
            await client._get(
                "/v5/market/tickers",
                {"category": "linear"},
            )
        message = str(exc.value)
        assert "api.bybit.com" in message
        assert "api.bytick.com" in message
        assert "HTTP 403" in message
    finally:
        await client.close()
