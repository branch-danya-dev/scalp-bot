"""Owner progress rules; broker stops, fees and partial execution stay independent."""
from ..strategy_policy import no_follow_through_seconds


def should_exit_without_progress(config, clock, pos, gross_mark_original) -> bool:
    age = (clock.perf_counter_ns() / 1e9 - pos.opened_mono
           if config.exchange_clock_enabled else clock.time() - pos.opened_at)
    timeout = no_follow_through_seconds(
        config,
        pos.strategy,
    )
    if age < timeout or pos.initial_risk_usd <= 0:
        return False
    current_r = gross_mark_original / pos.initial_risk_usd
    adverse_r = max(0.0, -current_r)
    weak_start = (
        pos.mfe_r < config.no_follow_through_max_mfe_r
        and adverse_r >= config.early_cut_at_r
    )
    if pos.strategy != "level_breakout":
        return weak_start
    # An old favorable excursion is not permanent immunity from a failed
    # breakout. Use monotonic time in clock-enabled runs, like entry age.
    if config.exchange_clock_enabled:
        progress_age = clock.perf_counter_ns() / 1e9 - (
            pos.mfe_mono if pos.mfe_mono is not None else pos.opened_mono
        )
    else:
        progress_age = clock.time() - (
            pos.mfe_at if pos.mfe_at is not None else pos.opened_at
        )
    giveback_r = max(0.0, pos.mfe_r - current_r)
    stalled_giveback = progress_age >= timeout and giveback_r >= config.early_cut_at_r
    should_cut = weak_start or stalled_giveback
    if should_cut:
        pos.strategy_details["noFollowThroughExit"] = {
            "rule": "breakout_stalled_giveback_v1",
            "cause": "weak_start" if weak_start else "stalled_giveback",
            "ageSeconds": age, "sincePeakSeconds": max(0.0, progress_age),
            "peakR": pos.mfe_r, "currentR": current_r, "givebackR": giveback_r,
        }
    return should_cut
