from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .domain import Side


def _positive_float(value: object, default: float = 0.0) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return default
    return resolved if resolved > 0 else default


def _snap(value: float, step: float, *, up: bool) -> float:
    if value <= 0 or step <= 0:
        return float(value)
    raw = Decimal(str(value))
    increment = Decimal(str(step))
    units = (raw / increment).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR,
    )
    return float(units * increment)


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    status: str
    tick_size: float
    qty_step: float
    min_order_qty: float
    min_notional_value: float
    max_order_qty: float
    max_market_order_qty: float
    funding_interval_minutes: int
    max_leverage: float

    @property
    def tradeable(self) -> bool:
        return self.status == "Trading"

    @classmethod
    def from_bybit(cls, row: dict) -> "InstrumentSpec":
        price_filter = row.get("priceFilter") or {}
        lot_filter = row.get("lotSizeFilter") or {}
        leverage_filter = row.get("leverageFilter") or {}
        return cls(
            symbol=str(row.get("symbol") or ""),
            status=str(row.get("status") or ""),
            tick_size=_positive_float(price_filter.get("tickSize")),
            qty_step=_positive_float(lot_filter.get("qtyStep")),
            min_order_qty=_positive_float(
                lot_filter.get("minOrderQty")
            ),
            min_notional_value=_positive_float(
                lot_filter.get("minNotionalValue")
            ),
            max_order_qty=_positive_float(
                lot_filter.get("maxOrderQty")
            ),
            max_market_order_qty=_positive_float(
                lot_filter.get("maxMktOrderQty")
            ),
            funding_interval_minutes=max(
                0,
                int(row.get("fundingInterval") or 0),
            ),
            max_leverage=_positive_float(
                leverage_filter.get("maxLeverage")
            ),
        )

    def public(self) -> dict:
        return asdict(self)

    def maker_entry_price(
        self,
        price: float,
        side: Side,
    ) -> float:
        return _snap(
            price,
            self.tick_size,
            up=side == Side.SHORT,
        )

    def stop_price(
        self,
        price: float,
        side: Side,
    ) -> float:
        # Round away from entry so planned loss is never understated.
        return _snap(
            price,
            self.tick_size,
            up=side == Side.SHORT,
        )

    def target_price(
        self,
        price: float,
        side: Side,
    ) -> float:
        # Round reward toward entry so planned payoff is conservative.
        return _snap(
            price,
            self.tick_size,
            up=side == Side.SHORT,
        )

    def normalize_quantity(
        self,
        *,
        entry_price: float,
        requested_notional: float,
        market_order: bool,
    ) -> tuple[float, float] | None:
        if entry_price <= 0 or requested_notional <= 0:
            return None
        raw_qty = requested_notional / entry_price
        maximum = (
            self.max_market_order_qty
            if market_order
            else self.max_order_qty
        )
        if maximum > 0:
            raw_qty = min(raw_qty, maximum)
        quantity = _snap(
            raw_qty,
            self.qty_step,
            up=False,
        )
        normalized_notional = quantity * entry_price
        if quantity <= 0:
            return None
        if (
            self.min_order_qty > 0
            and quantity + 1e-12 < self.min_order_qty
        ):
            return None
        if (
            self.min_notional_value > 0
            and normalized_notional + 1e-9
            < self.min_notional_value
        ):
            return None
        return quantity, normalized_notional
