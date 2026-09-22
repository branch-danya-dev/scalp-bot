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
