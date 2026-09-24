from scalp_bot.domain import Side
from scalp_bot.instrument import InstrumentSpec


def test_instrument_spec_parses_bybit_linear_filters() -> None:
    spec = InstrumentSpec.from_bybit({
        "symbol": "BTCUSDT",
        "status": "Trading",
        "fundingInterval": 480,
        "priceFilter": {"tickSize": "0.10"},
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "minNotionalValue": "5",
            "maxOrderQty": "1190",
            "maxMktOrderQty": "500",
        },
        "leverageFilter": {"maxLeverage": "100"},
    })

    assert spec.tradeable is True
    assert spec.tick_size == 0.10
    assert spec.qty_step == 0.001
    assert spec.min_notional_value == 5
    assert spec.max_market_order_qty == 500


def test_instrument_price_rounding_is_conservative() -> None:
    spec = InstrumentSpec(
        symbol="TESTUSDT",
        status="Trading",
        tick_size=0.10,
        qty_step=0.25,
        min_order_qty=0.25,
        min_notional_value=5,
        max_order_qty=100,
        max_market_order_qty=50,
        funding_interval_minutes=480,
        max_leverage=20,
    )

    assert spec.stop_price(99.87, Side.LONG) == 99.8
    assert spec.target_price(101.07, Side.LONG) == 101.0
    assert spec.stop_price(100.13, Side.SHORT) == 100.2
    assert spec.target_price(98.93, Side.SHORT) == 99.0
    assert spec.maker_entry_price(100.07, Side.LONG) == 100.0
    assert spec.maker_entry_price(99.93, Side.SHORT) == 100.0


def test_instrument_quantity_is_floored_to_step_and_market_cap() -> None:
    spec = InstrumentSpec(
        symbol="TESTUSDT",
        status="Trading",
        tick_size=0.01,
        qty_step=0.25,
        min_order_qty=0.25,
        min_notional_value=5,
        max_order_qty=10,
        max_market_order_qty=2,
        funding_interval_minutes=480,
        max_leverage=20,
    )

    quantity, notional = spec.normalize_quantity(
        entry_price=100,
        requested_notional=1000,
        market_order=True,
    ) or (0.0, 0.0)

    assert quantity == 2.0
    assert notional == 200.0
