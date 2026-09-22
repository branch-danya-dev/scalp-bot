from scalp_bot.research import OfflineStrategyReplay


def test_offline_replay_accepts_recorded_research_frames() -> None:
    rows = [
        {
            "ts": 1.0,
            "event": "symbol_activated",
            "symbol": "AAAUSDT",
            "payload": {
                "market": {
                    "candles": [
                        {
                            "time": i * 60,
                            "open": 100.0,
                            "high": 100.1,
                            "low": 99.9,
                            "close": 100.0,
                            "volume": 100,
                            "turnover": 10000,
                            "confirmed": True,
                        }
                        for i in range(80)
                    ]
                }
            },
        },
        {
            "ts": 2.0,
            "event": "research_frame",
            "symbol": "AAAUSDT",
            "payload": {
                "trend": "flat",
                "candle": {
                    "time": 80 * 60,
                    "open": 100.0,
                    "high": 100.1,
                    "low": 99.9,
                    "close": 100.0,
                    "volume": 100,
                    "turnover": 10000,
                    "confirmed": True,
                },
                "orderbook": {
                    "bids": [[99.99, 10, 999.9]],
                    "asks": [[100.01, 10, 1000.1]],
                },
                "recentTrades": [],
                "structure": {
                    "levels": [],
                    "trendlines": [],
                    "dayHigh": None,
                    "dayLow": None,
                    "previousDayHigh": None,
                    "previousDayLow": None,
                },
            },
        },
    ]
    signals = OfflineStrategyReplay().run_rows(rows)
    assert isinstance(signals, list)
