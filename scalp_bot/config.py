from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCALP_", env_file=".env", extra="ignore")

    bybit_rest_url: str = "https://api.bybit.com"
    bybit_public_ws_url: str = "wss://stream.bybit.com/v5/public/linear"

    start_balance: float = 1_000.0
    min_turnover_usd: float = 150_000_000.0
    liquid_universe_size: int = 30
    working_symbols: int = 4
    max_active_symbols: int = 8
    active_keep_rank: int = 12
    active_symbol_min_seconds: float = 600.0
    active_symbol_idle_timeout_seconds: float = 300.0
    activity_window_minutes: int = 5
    activity_correlation_window_minutes: int = 60
    activity_benchmark_symbol: str = "BTCUSDT"
    scanner_interval_seconds: float = 60.0
    activity_request_concurrency: int = 2
    activity_request_pause_seconds: float = 0.20
    bootstrap_1m_limit: int = 720
    bootstrap_15m_limit: int = 480
    rest_request_min_interval_seconds: float = 0.25
    rest_rate_limit_retries: int = 6
    rest_rate_limit_backoff_seconds: float = 1.0
    rest_rate_limit_max_backoff_seconds: float = 8.0
    empty_startup_rescan_seconds: float = 10.0

    min_net_profit_usd: float = 0.10
    min_net_reward_risk: float = 1.15
    enforce_net_reward_risk_gate: bool = False
    risk_fraction: float = 0.005
    max_total_risk_fraction: float = 0.02
    max_leverage: float = 1.0
    max_open_positions: int = 4
    max_position_exposure_fraction: float = 0.25
    max_daily_loss_fraction: float = 0.03
    enforce_session_loss_limit: bool = False
    max_entry_drift_bps: float = 8.0
    taker_fee_rate: float = 0.00055
    maker_fee_rate: float = 0.00020
    slippage_bps: float = 1.0

    partial_take_enabled: bool = True
    partial_take_at_r: float = 1.0
    partial_take_fraction: float = 0.70
    runner_target_r: float = 2.5
    breakeven_buffer_bps: float = 1.0
    no_follow_through_seconds: float = 20.0
    no_follow_through_max_mfe_r: float = 0.25
    early_cut_at_r: float = 0.45

    setup_rearm_seconds: float = 20.0
    setup_reset_wait_seconds: float = 10.0
    arbiter_interval_seconds: float = 0.25
    market_stale_seconds: float = 3.0

    run_label: str = "paper-current-10h"
    paper_run_duration_seconds: float = 36_000.0
    replay_engaged_frame_seconds: float = 1.0
    replay_idle_frame_seconds: float = 5.0
    replay_book_depth: int = 16
    session_dir: str = "data/sessions"


settings = Settings()
