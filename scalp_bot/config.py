from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCALP_", env_file=".env", extra="ignore")

    bybit_rest_url: str = "https://api.bybit.com"
    bybit_rest_fallback_urls: str = "https://api.bytick.com"
    bybit_public_ws_url: str = "wss://stream.bybit.com/v5/public/linear"

    start_balance: float = 1_000.0
    min_turnover_usd: float = 150_000_000.0
    liquid_universe_size: int = 30
    working_symbols: int = 6
    max_active_symbols: int = 12
    active_keep_rank: int = 12
    active_symbol_min_seconds: float = 600.0
    active_symbol_idle_timeout_seconds: float = 300.0
    activity_window_minutes: int = 5
    activity_correlation_window_minutes: int = 60
    activity_benchmark_symbol: str = "BTCUSDT"
    scanner_interval_seconds: float = 60.0
    activity_request_concurrency: int = 2
    activity_request_pause_seconds: float = 0.20
    bootstrap_1m_candles: int = 720
    bootstrap_5m_candles: int = 576
    bootstrap_15m_candles: int = 480
    bootstrap_1h_candles: int = 336
    rest_request_min_interval_seconds: float = 0.25
    rest_rate_limit_retries: int = 6
    rest_rate_limit_backoff_seconds: float = 1.0
    rest_rate_limit_max_backoff_seconds: float = 8.0
    empty_startup_rescan_seconds: float = 10.0

    min_net_profit_usd: float = 1.00
    min_net_profit_equity_fraction: float = 0.001
    enforce_min_net_profit_gate: bool = True
    min_net_reward_risk: float = 1.15
    enforce_net_reward_risk_gate: bool = True
    # Structural price risk to the strategy invalidation point.
    risk_fraction: float = 0.005
    # Maximum planned stop loss including fees/slippage for one position.
    max_trade_all_in_loss_fraction: float = 0.0125
    max_total_risk_fraction: float = 0.02
    # max_leverage is the aggregate gross portfolio exposure cap.
    max_leverage: float = 10.0
    # A single tight-stop scalp may use materially more notional than equity,
    # but it cannot consume the whole portfolio leverage budget.
    max_position_leverage: float = 5.0
    max_open_positions: int = 4
    max_position_exposure_fraction: float = 1.0
    max_daily_loss_fraction: float = 0.03
    enforce_session_loss_limit: bool = False

    strategy_expectancy_min_samples: int = 30
    enforce_strategy_expectancy_gate: bool = False
    trend_structure_min_expectancy_r: float = 0.0
    weak_level_rejection_min_expectancy_r: float = 0.0
    density_min_expectancy_r: float = 0.0
    breakout_min_expectancy_r: float = 0.0
    max_entry_drift_bps: float = 8.0
    taker_fee_rate: float = 0.00055
    maker_fee_rate: float = 0.00020
    slippage_bps: float = 1.0
    maker_fill_confirmation_bps: float = 0.5
    passive_entry_enabled: bool = False
    passive_entry_timeout_seconds: float = 15.0
    max_winner_cost_share: float = 0.35
    enforce_winner_cost_share_gate: bool = False
    max_stop_cost_share: float = 1.0
    enforce_stop_cost_share_gate: bool = False

    partial_take_enabled: bool = True
    partial_take_at_r: float = 1.0
    partial_take_fraction: float = 0.70
    trend_structure_partial_take_fraction: float = 0.50
    weak_level_rejection_partial_take_fraction: float = 0.70
    density_partial_take_fraction: float = 0.70
    breakout_partial_take_fraction: float = 0.30
    runner_target_r: float = 2.5
    breakeven_buffer_bps: float = 1.0
    no_follow_through_seconds: float = 20.0
    trend_structure_no_follow_through_seconds: float = 45.0
    weak_level_rejection_no_follow_through_seconds: float = 45.0
    density_no_follow_through_seconds: float = 20.0
    breakout_no_follow_through_seconds: float = 120.0
    no_follow_through_max_mfe_r: float = 0.25
    early_cut_at_r: float = 0.45

    setup_rearm_seconds: float = 20.0
    setup_reset_wait_seconds: float = 10.0
    arbiter_interval_seconds: float = 0.25
    market_stale_seconds: float = 3.0
    book_stale_seconds: float = 1.5
    trade_buffer_seconds: int = 90
    orderbook_depth: int = 1000
    density_min_wall_notional_usd: float = 25_000.0
    density_strength_multiple: float = 4.0
    density_turnover_floor_fraction: float = 0.01
    density_neighbor_window_levels: int = 20
    density_max_distance_pct: float = 0.05
    research_frame_seconds: float = 1.0
    research_recent_trades: int = 250
    replay_recent_trades: int = 250

    run_label: str = "paper-v3-scalp-econ-4h"
    paper_run_duration_seconds: float = 14_400.0
    replay_engaged_frame_seconds: float = 1.0
    replay_idle_frame_seconds: float = 5.0
    replay_book_depth: int = 16
    session_dir: str = "data/sessions"


settings = Settings()
