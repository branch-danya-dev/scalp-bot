from dataclasses import replace
import pytest
from scalp_bot.expectancy import SegmentExpectancyBook
from scalp_bot.segment_registry import SegmentRegistry, RegistryPolicy
from scalp_bot.setup_segments import DIMENSIONS

SEGMENT = dict.fromkeys(DIMENSIONS, "known")


def add(book, index, value=1, **extra):
    return book.record_shadow(dict(identity=str(index), capture_id=str(index//20), symbol="AAA",
        segment=SEGMENT, netPnl=value, initialRiskUsd=1, executable=True,
        available_wall_ms=index*2, label_end_wall_ms=index*2+1, **extra))


def test_registry_replay_residence_hysteresis_and_dormant_recovery():
    def replay():
        book = SegmentExpectancyBook()
        registry = SegmentRegistry(book, replace(RegistryPolicy(), minimum_residence_ms=10))
        states = [registry.assess(SEGMENT, now_ms=0)["wouldState"]]
        for i in range(100):
            assert add(book, i)
        states.append(registry.assess(SEGMENT, now_ms=200)["wouldState"])
        states.append(registry.assess(SEGMENT, now_ms=201)["wouldState"])
        states.append(registry.assess(SEGMENT, now_ms=210)["wouldState"])
        for i in range(100, 115):
            add(book, i, -1)
        states.append(registry.assess(SEGMENT, now_ms=240)["wouldState"])
        for i in range(115, 315):
            add(book, i, 1)
        result = registry.assess(SEGMENT, now_ms=640)
        states.append(result["wouldState"])
        assert result["appliedRiskScale"] == 1 and result["redistributesRisk"] is False
        assert book.stats == {}  # labels never become closed-position portfolio evidence
        return states
    assert replay() == replay() == ["OBSERVING", "TRIAL", "TRIAL", "ACTIVE", "DORMANT", "TRIAL"]


def test_bad_duplicate_censored_and_overlapping_shadow_evidence_excluded():
    book = SegmentExpectancyBook()
    assert add(book, 0)
    assert not add(book, 0)
    assert not add(book, 1, censor_reason="depth_gap")
    assert not add(book, 2, float("nan"))
    assert add(book, 3)
    assert not add(book, 2)  # late interval cannot be fitted retroactively
    assert book.lifecycle_evidence(SEGMENT)["longTerm"]["samples"] == 2


def test_unknown_and_single_capture_cannot_promote_and_clock_cannot_regress():
    book = SegmentExpectancyBook()
    reg = SegmentRegistry(book)
    for i in range(10):
        add(book, i)
    assert reg.assess(SEGMENT, now_ms=100)["wouldState"] == "OBSERVING"
    with pytest.raises(ValueError, match="regressed"):
        reg.assess(SEGMENT, now_ms=99)
