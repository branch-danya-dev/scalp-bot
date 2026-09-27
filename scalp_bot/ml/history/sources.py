"""Explicit, versioned archive descriptions; URL construction is not coverage proof."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import re


PROVIDERS = {
    "bybit-public-trades": ("bybit_csv_v1", "trades", False),
    "tardis-trades": ("tardis_csv_v1", "trades", True),
    "tardis-l2": ("tardis_csv_v1", "incremental_book_L2", True),
}


@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    provider: str
    symbol: str
    day: str
    purpose: str = "development"
    market: str = "linear_perpetual"
    schema_version: int = 1

    def __post_init__(self):
        if not isinstance(self.provider, str) or self.provider not in PROVIDERS:
            raise ValueError("unsupported provider")
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,40}USDT", self.symbol):
            raise ValueError("M1a accepts explicit single USDT-linear symbols only")
        if not isinstance(self.day, str):
            raise ValueError("day must be ISO text")
        parsed = date.fromisoformat(self.day)
        if parsed.isoformat() != self.day or not 2009 <= parsed.year < 2100:
            raise ValueError("day must be ISO YYYY-MM-DD in 2009..2099")
        if self.market != "linear_perpetual" or type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported market or schema version; do not mix spot/inverse")
        if self.purpose not in {"development", "format_smoke_only", "reserved_holdout"}:
            raise ValueError("unknown dataset purpose")

    @property
    def url(self) -> str:
        if self.provider == "bybit-public-trades":
            return f"https://public.bybit.com/trading/{self.symbol}/{self.symbol}{self.day}.csv.gz"
        return (f"https://datasets.tardis.dev/v1/bybit/{PROVIDERS[self.provider][1]}/"
                f"{self.day.replace('-', '/')}/{self.symbol}.csv.gz")

    @property
    def has_arrival_time(self) -> bool:
        return PROVIDERS[self.provider][2]

    @property
    def day_start_us(self) -> int:
        return int(datetime.fromisoformat(self.day).replace(tzinfo=timezone.utc).timestamp()) * 1_000_000

    def public(self) -> dict:
        return {**asdict(self), "url": self.url, "adapter_schema": PROVIDERS[self.provider][0],
                "quantity_unit": "exchange_native", "contract_multiplier_verified": False, "availability_verified": False,
                "receiver_clock": "tardis_collector" if self.has_arrival_time else None,
                "scanner_membership_reconstructed": False,
                "license": "provider terms must be reviewed; public access is not a redistribution grant"}

    @classmethod
    def from_public(cls, value: dict):
        if not isinstance(value, dict):
            raise ValueError("source descriptor must be an object")
        spec = cls(**{key: value[key] for key in ("provider", "symbol", "day", "purpose", "market", "schema_version")})
        # The supplied document cannot redirect downloads or silently change capabilities.
        if value != spec.public():
            raise ValueError("source descriptor differs from supported version; regenerate with plan")
        return spec
