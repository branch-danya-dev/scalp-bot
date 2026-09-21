from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCALP_", env_file=".env", extra="ignore")

    bybit_rest_url: str = "https://api.bybit.com"
    bybit_public_ws_url: str = "wss://stream.bybit.com/v5/public/linear"

    start_balance: float = 1_000.0
    min_turnover_usd: float = 150_000_000.0
    working_symbols: int = 3
    scanner_interval_seconds: float = 45.0

    min_net_profit_usd: float = 1.0
    risk_fraction: float = 0.005
    max_leverage: float = 1.0
    taker_fee_rate: float = 0.00055
    maker_fee_rate: float = 0.00020
    slippage_bps: float = 1.0

    session_dir: str = "data/sessions"


settings = Settings()
