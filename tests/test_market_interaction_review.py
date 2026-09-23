from scalp_bot.market_interaction_review import analyze_market_interactions


def frame(ts: float, symbol: str, price: float, *, candle_high: float | None = None) -> dict:
    return {
        "ts": ts,
        "event": "research_frame",
        "symbol": symbol,
        "payload": {
            "lastPrice": price,
            "candle": {
                "open": price,
                "high": candle_high if candle_high is not None else price,
                "low": price,
                "close": price,
            },
        },
    }


def decision(
    ts: float,
    symbol: str,
    strategy: str,
    state: str,
    *,
    label: str,
    low: float | None = None,
    high: float | None = None,
    price: float | None = None,
    action: str = "wait",
    trend: str = "up",
    details: dict | None = None,
) -> dict:
    obj = {
        "type": "horizontal_zone" if low is not None else "orderbook_wall",
        "label": label,
    }
    if low is not None and high is not None:
        obj["low"] = low
        obj["high"] = high
    if price is not None:
        obj["price"] = price

    payload_details = {"state": state}
    if details:
        payload_details.update(details)

    return {
        "ts": ts,
        "event": "decision",
        "symbol": symbol,
        "payload": {
            "strategy": strategy,
            "action": action,
            "confidence": 0.7,
            "details": payload_details,
            "trace": {
                "state": state,
                "trend": trend,
                "object": obj,
                "evidence": {},
            },
        },
    }


def test_break_checkpoint_uses_sampled_last_price_and_deduplicates_unchanged_state() -> None:
    rows = [
        frame(99.0, "AAAUSDT", 100.0),
        decision(
            100.0,
            "AAAUSDT",
            "level_breakout",
            "break",
            label="resistance",
            low=99.9,
            high=100.1,
        ),
        decision(
            101.0,
            "AAAUSDT",
            "level_breakout",
            "break",
            label="resistance",
            low=99.9,
            high=100.1,
        ),
        # The candle high deliberately contains a stale forming-candle extreme.
        # Research classification must use lastPrice=100.0, not high=101.0.
        frame(105.0, "AAAUSDT", 100.0, candle_high=101.0),
        frame(110.0, "AAAUSDT", 100.25),
    ]

    report = analyze_market_interactions(
        rows,
        horizons_seconds=(30.0,),
        move_bands=(0.002,),
    )

    assert report["summary"]["checkpoints"] == 1
    checkpoint = report["checkpoints"][0]
    band = checkpoint["forward"]["30s"]["movementBands"]["0.20%"]
    assert band["classification"] == "hypothesis_first"
    assert band["secondsToHypothesis"] == 10.0


def test_same_level_breakout_and_rejection_are_recorded_as_conflict() -> None:
    rows = [
        frame(99.0, "AAAUSDT", 100.0),
        decision(
            100.0,
            "AAAUSDT",
            "level_breakout",
            "pressure",
            label="resistance",
            low=99.9,
            high=100.1,
        ),
        decision(
            101.0,
            "AAAUSDT",
            "weak_level_rejection",
            "test",
            label="resistance",
            low=99.9,
            high=100.1,
        ),
        frame(105.0, "AAAUSDT", 100.3),
    ]

    report = analyze_market_interactions(
        rows,
        horizons_seconds=(30.0,),
        move_bands=(0.002,),
    )

    assert report["summary"]["conflicts"] == 1
    overlap = report["overlaps"][0]
    assert overlap["relationship"] == "conflict"
    band = overlap["forward"]["30s"]["movementBands"]["0.20%"]
    assert band["classification"] == "up_first"
    assert band["winnerStrategy"] == "level_breakout"


def test_density_checkpoint_preserves_pre_entry_liquidity_features() -> None:
    rows = [
        frame(99.0, "AAAUSDT", 100.0),
        decision(
            100.0,
            "AAAUSDT",
            "orderbook_density",
            "reaction",
            label="ask density",
            price=100.1,
            action="short",
            details={
                "wallSide": "ask",
                "wallPrice": 100.1,
                "remainingRatio": 0.13,
                "strengthMultiple": 0.78,
                "depletionPerSecond": 0.12,
                "replenishmentRatio": 0.0,
                "wallPresent": True,
                "flow": {
                    "imbalance5s": -1.0,
                    "participationConfirmed": True,
                },
            },
        ),
        frame(110.0, "AAAUSDT", 99.7),
    ]

    report = analyze_market_interactions(
        rows,
        horizons_seconds=(30.0,),
        move_bands=(0.002,),
    )

    features = report["checkpoints"][0]["features"]
    assert features["remainingRatio"] == 0.13
    assert features["strengthMultiple"] == 0.78
    assert features["depletionPerSecond"] == 0.12
    assert features["flowImbalance5s"] == -1.0


def test_non_tracked_state_resets_checkpoint_identity_for_reentry() -> None:
    rows = [
        frame(99.0, "AAAUSDT", 100.0),
        decision(
            100.0,
            "AAAUSDT",
            "level_breakout",
            "break",
            label="resistance",
            low=99.9,
            high=100.1,
        ),
        decision(
            105.0,
            "AAAUSDT",
            "level_breakout",
            "search",
            label="market context",
            price=100.0,
        ),
        decision(
            110.0,
            "AAAUSDT",
            "level_breakout",
            "break",
            label="resistance",
            low=99.9,
            high=100.1,
        ),
        frame(115.0, "AAAUSDT", 100.25),
    ]

    report = analyze_market_interactions(
        rows,
        horizons_seconds=(30.0,),
        move_bands=(0.002,),
    )

    assert report["summary"]["checkpoints"] == 2



def test_density_checkpoint_is_not_counted_as_playbook_conflict() -> None:
    rows = [
        frame(99.0, "AAAUSDT", 100.0),
        decision(
            100.0,
            "AAAUSDT",
            "orderbook_density",
            "reaction",
            label="ask density",
            price=100.1,
            action="short",
            details={
                "wallSide": "ask",
                "wallPrice": 100.1,
            },
        ),
        decision(
            101.0,
            "AAAUSDT",
            "level_breakout",
            "break",
            label="resistance",
            low=99.9,
            high=100.1,
            action="long",
        ),
        frame(110.0, "AAAUSDT", 100.3),
    ]

    report = analyze_market_interactions(
        rows,
        horizons_seconds=(30.0,),
        move_bands=(0.002,),
    )

    assert report["summary"]["liquidityEvidenceCheckpoints"] == 1
    assert report["summary"]["overlaps"] == 0
    assert report["summary"]["conflicts"] == 0
