import os

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


_DISABLE_DOTENV = os.getenv(
    "SCALP_DISABLE_DOTENV",
    "",
).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCALP_",
        env_file=None if _DISABLE_DOTENV else ".env",
        extra="ignore",
    )

    bybit_rest_url: str = "https://api.bybit.com"
    bybit_rest_fallback_urls: str = "https://api.bytick.com"
    bybit_public_ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    # Optional read-only credentials enable exact account fee-rate lookup.
    # Never place secrets in checked-in profiles; provide them as process env.
    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    bybit_private_recv_window_ms: int = 5000
    fee_rate_mode: str = "account_if_available"
    # Legacy deterministic replay/tests may opt out; checked-in runtime profile enables this.
    exchange_clock_enabled: bool = False
    clock_sync_interval_seconds: float = 20.0
    clock_max_rtt_ms: float = 400.0
    clock_max_sync_age_seconds: float = 60.0
    clock_max_uncertainty_ms: float = 250.0
    clock_wall_jump_ms: float = 250.0
    clock_drift_ppm: float = 50.0
    trade_receipt_stale_seconds: float = 5.0

    start_balance: float = 1_000.0
    e01_breakout_obstacle_veto: bool = False
    e06_conditional_breakout_hold: bool = False
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
    market_preflight_timeout_seconds: float = 30.0

    min_net_profit_usd: float = 1.00
    min_net_profit_equity_fraction: float = 0.001
    enforce_min_net_profit_gate: bool = True
    min_net_reward_risk: float = 1.15
    enforce_net_reward_risk_gate: bool = True
    # Hard safety floor: research profiles may shadow/tune the stricter
    # payoff gate, but the bot must never knowingly enter negative payoff
    # geometry where the planned net winner is smaller than the planned loss.
    absolute_min_net_reward_risk: float = 1.0
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
    # Pending maker reservations have a separate concurrency cap. They still
    # reserve exposure/risk, but no longer consume actual position slots.
    max_pending_entries: int = 4
    max_position_exposure_fraction: float = 1.0
    max_daily_loss_fraction: float = 0.03
    enforce_session_loss_limit: bool = False

    trend_structure_enabled: bool = False
    weak_level_rejection_enabled: bool = True
    # Stage 4: retained name for env compatibility; enables the liquidity
    # evidence provider, not standalone density entries.
    density_enabled: bool = True
    breakout_enabled: bool = True

    # Staged-entry infrastructure remains available for controlled research,
    # but Stage 19 production policy disables it for both live playbooks: the
    # Stage 17 add legs amplified false breakouts and late rejection entries.
    staged_entries_enabled: bool = True
    breakout_staged_entries_enabled: bool = False
    weak_level_rejection_staged_entries_enabled: bool = False
    breakout_probe_risk_fraction: float = 0.35
    weak_level_rejection_probe_risk_fraction: float = 0.30
    breakout_retest_tolerance_bps: float = 3.0
    breakout_retest_response_min_bps: float = 2.0
    breakout_hold_without_retest_seconds: float = 8.0
    # Legacy manifest compatibility; the rolling 15s absorption veto is retired.
    breakout_absorption_efficiency_threshold: float = 0.35
    breakout_min_directional_response_bps: float = 2.0
    weak_level_rejection_micro_response_min_bps: float = 1.5
    weak_level_rejection_micro_response_min_seconds: float = 0.50
    weak_level_rejection_micro_response_max_seconds: float = 6.0

    strategy_expectancy_min_samples: int = 30
    enforce_strategy_expectancy_gate: bool = False
    # Stage 9 research-only readiness guards. These values do not block
    # trading; they only decide when conditional economic calibration has
    # enough observations to be interpreted.
    economic_calibration_min_group_samples: int = 20
    economic_calibration_min_segment_samples: int = 8
    # Stage 12 research-policy promotion is opt-in. "off" ignores any
    # configured policy file, "shadow" records would-block matches, and
    # "enforce" requires a manifest explicitly created with allowEnforce=true.
    research_policy_mode: str = "off"
    research_policy_file: str = ""
    trend_structure_min_expectancy_r: float = 0.0
    weak_level_rejection_min_expectancy_r: float = 0.0
    density_min_expectancy_r: float = 0.0
    breakout_min_expectancy_r: float = 0.0
    max_entry_drift_bps: float = 8.0
    taker_fee_rate: float = 0.00055
    maker_fee_rate: float = 0.00020
    slippage_bps: float = 1.0
    maker_fill_confirmation_bps: float = 0.5
    maker_queue_ahead_fraction: float = 0.50
    passive_entry_enabled: bool = False
    passive_entry_timeout_seconds: float = 15.0
    max_winner_cost_share: float = 0.35
    enforce_winner_cost_share_gate: bool = False
    min_first_take_move_pct: float = 0.003
    enforce_min_first_take_move_gate: bool = False
    max_stop_cost_share: float = 1.0
    enforce_stop_cost_share_gate: bool = False
    stop_depth_stress_multiplier: float = 2.0
    # When requested market exit size exceeds visible deep-book depth, never
    # assume the missing tail is available at the last visible level.
    paper_missing_depth_penalty_bps: float = 25.0

    partial_take_enabled: bool = True
    partial_take_at_r: float = 1.0
    partial_take_fraction: float = 0.70
    trend_structure_partial_take_fraction: float = 0.50
    # Rejection edge appears at REJECT, while taking 70% at 1R made the
    # planned winner smaller than the all-in loser after costs. Keep more
    # notional for the structural target.
    weak_level_rejection_partial_take_fraction: float = 0.30
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
    # Keep idle symbols cheap, but evaluate engaged setups much faster so
    # ARMED -> confirmation -> FIRE latency is driven by market evidence
    # instead of the old fixed 0.8s polling cadence.
    evaluation_idle_interval_seconds: float = 0.8
    evaluation_engaged_interval_seconds: float = 0.20
    arbiter_interval_seconds: float = 0.25
    # Strategy-specific manage_position logic already requires persistent
    # invalidation. Keep only a tiny post-fill guard instead of the previous
    # hardcoded 5s blind window.
    strategy_invalidation_grace_seconds: float = 0.50
    # market_context_changed is diagnostic telemetry, not the trading clock.
    # Full causal context is already attached to decisions/state transitions
    # and research frames; throttle minor context churn to keep long sessions
    # tractable.
    market_context_event_interval_seconds: float = 10.0
    market_stale_seconds: float = 3.0
    book_stale_seconds: float = 1.5
    deep_book_stale_seconds: float = 1.5
    # L1000 is intentionally slower than L50, but execution/risk must not mix
    # a fresh fast quote with a materially older depth snapshot.
    deep_book_max_skew_seconds: float = 0.50
    # Disabled in bare Settings for deterministic unit tests; research/live
    # profiles explicitly enable this safety gate.
    confirmed_candle_stale_seconds: float = 0.0
    trade_buffer_seconds: int = 90
    # Legacy compatibility value. Live market data uses the explicit fast/deep
    # depths below so execution timing no longer waits on the 1000-level feed.
    orderbook_depth: int = 1000
    fast_orderbook_depth: int = 50
    deep_orderbook_depth: int = 1000
    event_driven_evaluation_enabled: bool = True
    event_evaluation_min_interval_seconds: float = 0.05
    market_queue_size: int = 512
    market_queue_put_timeout_seconds: float = 0.05
    market_queue_max_lag_seconds: float = 0.50

    prometheus_enabled: bool = True
    otel_enabled: bool = False
    otel_service_name: str = "scalp-bot"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4318"
    otel_trace_sample_ratio: float = 1.0
    fast_event_min_mid_move_bps: float = 0.25
    fast_event_min_spread_change_bps: float = 0.25
    fast_event_min_ofi_fraction: float = 0.02
    density_min_wall_notional_usd: float = 25_000.0
    density_strength_multiple: float = 4.0
    density_turnover_floor_fraction: float = 0.01
    density_neighbor_window_levels: int = 20
    density_max_distance_pct: float = 0.05
    research_frame_seconds: float = 1.0
    research_recent_trades: int = 250
    replay_recent_trades: int = 250
    research_trade_delta_enabled: bool = False
    replay_trade_delta_enabled: bool = False
    recorder_queue_size: int = 8192
    recorder_critical_enqueue_timeout_seconds: float = 0.01

    run_label: str = "paper-v3-scalp-econ-4h"
    paper_run_duration_seconds: float = 14_400.0
    replay_engaged_frame_seconds: float = 1.0
    replay_idle_frame_seconds: float = 5.0
    replay_book_depth: int = 16
    session_dir: str = "data/sessions"


settings = Settings()
