import pytest

from scalp_bot.market_clock import MarketClock, receipt_age_seconds


def synced(**kwargs):
    clock = MarketClock(**kwargs)
    assert clock.synchronize(server_ms=1_000_000, sent_mono=10,
                             received_mono=10.1, received_wall_ms=999_000)
    return clock


def test_no_unsynchronized_fallback_to_local_clock():
    value = MarketClock().read(mono=10, wall_ms=500_000)
    assert not value.valid
    assert value.reason == "unsynchronized"
    assert value.evaluation_ms is None


def test_received_trade_ahead_of_local_clock_is_in_exchange_bounds():
    value = synced().read(mono=10.2, wall_ms=999_100, latest_processed_event_ms=1_000_150)
    assert value.valid
    assert value.exchange_lower_ms < 1_000_100 < value.exchange_upper_ms
    assert value.evaluation_ms >= 1_000_150
    assert 50 <= value.uncertainty_ms <= 52


def test_future_trade_does_not_pull_clock_forward():
    c = synced()
    value = c.read(mono=10.2, wall_ms=999_100, latest_processed_event_ms=2_000_000)
    assert value.reason == "event_outside_clock_bound"
    assert value.evaluation_ms is None
    assert value.exchange_upper_ms < 1_001_000


@pytest.mark.parametrize("wall", [998_000, 1_001_000])
def test_wall_jump_requires_new_sync_and_does_not_change_exchange_bounds(wall):
    c = synced()
    value = c.read(mono=10.2, wall_ms=wall)
    assert value.reason == "local_wall_jump"
    assert value.exchange_upper_ms < 1_001_000
    assert not c.read(mono=10.3, wall_ms=wall + 100).valid
    assert c.synchronize(server_ms=1_000_300, sent_mono=10.3,
                         received_mono=10.4, received_wall_ms=wall + 200)
    assert c.read(mono=10.4, wall_ms=wall + 200).valid


def test_pause_expires_sync_without_stale_becoming_fresh():
    value = synced().read(mono=80.1, wall_ms=1_069_000)
    assert value.reason == "synchronization_expired"
    assert receipt_age_seconds(now_mono=80.1, received_mono=10.1) == 70


def test_monotonic_rollback_is_invalid_not_clamped_to_zero():
    assert synced().read(mono=9, wall_ms=997_900).reason == "monotonic_rollback"
    assert receipt_age_seconds(now_mono=9, received_mono=10) is None


def test_slow_sync_preserves_good_anchor_only_until_original_expiry():
    c = synced()
    assert not c.synchronize(server_ms=1_000_500, sent_mono=11,
                             received_mono=12, received_wall_ms=1_000_900)
    assert c.last_sync_rejection == "sync_rtt_exceeded"
    assert c.read(mono=12, wall_ms=1_000_900).valid
    assert c.read(mono=71.1, wall_ms=1_060_000).reason == "synchronization_expired"


def test_slow_sync_never_clears_a_wall_jump_fault():
    c = synced()
    assert not c.read(mono=10.2, wall_ms=1_999_100).valid
    assert not c.synchronize(server_ms=1_000_500, sent_mono=11,
                             received_mono=12, received_wall_ms=2_000_900)
    assert c.read(mono=12, wall_ms=2_000_900).reason == "local_wall_jump"


def test_resync_cannot_move_evaluation_backwards():
    c = synced()
    assert c.read(mono=11.1, wall_ms=1_000_000).valid
    assert not c.synchronize(server_ms=999_000, sent_mono=11.1,
                             received_mono=11.2, received_wall_ms=1_000_100)
    assert c.read(mono=11.2, wall_ms=1_000_100).reason == "exchange_clock_rollback"


@pytest.mark.parametrize("old,new", [
    ((1790326209920.1277, 433378.8893714, 433379.2209273, 1790326209669.8718),
     (1790326236088.9338, 433405.1215085, 433405.3883791, 1790326235837.3237)),
    ((1790326256443.367, 433425.3899805, 433425.7422795, 1790326256191.2249),
     (1790326276790.6885, 433445.7437962, 433446.1100495, 1790326276558.994)),
])
def test_recorded_smoke_overlapping_sync_does_not_latch_rollback(old, new):
    # Actual samples from session-20260925T084703Z. Model a market read just
    # before the REST response; high-frequency reads are not all recorded.
    c = MarketClock()
    names = ("server_ms", "sent_mono", "received_mono", "received_wall_ms")
    assert c.synchronize(**dict(zip(names, old)))
    before = c.read(mono=new[2] - .000001, wall_ms=new[3] - .001)
    assert before.valid
    assert c.synchronize(**dict(zip(names, new)))
    after = c.read(mono=new[2], wall_ms=new[3])
    assert after.valid
    assert after.evaluation_ms >= before.evaluation_ms
    assert 0 < c.last_sync_upper_padding_ms < 70
    assert after.uncertainty_ms <= c.max_uncertainty_ms
    assert c.read(mono=new[2] + 61, wall_ms=new[3] + 61000).reason == "synchronization_expired"


def test_overlap_padding_cannot_exceed_uncertainty_budget():
    c = synced()
    before = c.read(mono=10.2, wall_ms=999_100)
    c.max_uncertainty_ms = 100
    assert not c.synchronize(server_ms=1_000_000, sent_mono=10.051,
                             received_mono=10.201, received_wall_ms=999_101)
    assert c.last_sync_rejection == "clock_uncertainty_exceeded"
    after = c.read(mono=10.201, wall_ms=999_101)
    assert after.valid and after.evaluation_ms >= before.evaluation_ms
    assert after.sync_age_seconds == pytest.approx(.101)


def test_expired_anchor_cannot_justify_backward_padding():
    c = synced(max_sync_age_seconds=.1)
    c.read(mono=10.19, wall_ms=999_090)
    assert not c.synchronize(server_ms=1_000_050, sent_mono=10.2,
                             received_mono=10.3, received_wall_ms=999_200)
    assert c.last_sync_rejection == "exchange_clock_rollback"


def test_gradual_wall_drift_detected_against_sync_anchor():
    c = synced()
    assert c.read(mono=10.2, wall_ms=999_200).valid
    assert c.read(mono=10.3, wall_ms=999_400).valid
    assert c.read(mono=10.4, wall_ms=999_600).reason == "local_wall_drift"


def test_uncertainty_grows_with_sample_age():
    c = synced(drift_ppm=100_000, max_uncertainty_ms=200)
    assert c.read(mono=12.1, wall_ms=1_001_000).reason == "clock_uncertainty_exceeded"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_limits_rejected(value):
    with pytest.raises(ValueError):
        MarketClock(drift_ppm=value)
