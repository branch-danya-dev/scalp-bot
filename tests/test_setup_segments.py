from scalp_bot.expectancy import SegmentExpectancyBook
from scalp_bot.setup_segments import DIMENSIONS, setup_segment, summarize


def test_closed_outcomes_include_runner_target_and_keep_missing_flags_unknown():
    from scalp_bot.setup_segments import closed_observation
    row = closed_observation(dict(reason='runner_target'))
    assert row['target'] is True
    assert row['partial'] is None
    assert closed_observation({})['stop'] is None


def test_segment_is_entry_time_and_side_relative():
    details = {"entryContextAssessment": {"directionPlan": {
        "localRegime": "bearish_trend", "htfBias": "bearish"}, "flowClassification": "opposed"},
        "economics": {"winnerCostShare": .3}, "targetSource": "next_structural_level"}
    segment = setup_segment("level_breakout", "long", details, entry=100, stop=99.8)
    assert segment["trendRelation"] == "countertrend"
    assert segment["htfAlignment"] == "opposed"
    assert segment["costShareBucket"] == "lt_0.35"


def test_segment_gate_requires_valid_unique_closed_evidence_and_explicit_enforcement():
    book = SegmentExpectancyBook()
    segment = dict.fromkeys(DIMENSIONS, "known")
    first = dict(identity="one", segment=segment, netPnl=-2, initialRiskUsd=1)
    book.record(first)
    book.record(first)
    book.record(dict(first, identity="zero-risk", initialRiskUsd=0))
    args = dict(min_samples=2, minimum_expectancy_r=0)
    assert not book.assess(segment, mode="enforce", **args)["blocked"]
    book.record(dict(first, identity="two"))
    shadow = book.assess(segment, mode="shadow", **args)
    assert shadow["wouldVeto"] and not shadow["blocked"] and shadow["trades"] == 2
    assert book.assess(segment, mode="enforce", **args)["blocked"]
    unknown = dict(segment, flowAlignment="unknown")
    book.record(dict(first, identity="unknown1", segment=unknown))
    book.record(dict(first, identity="unknown2", segment=unknown))
    assert not book.assess(unknown, mode="enforce", **args)["blocked"]


def test_diagnostic_labels_cannot_be_portfolio_pnl_and_missing_r_is_not_zero():
    segment = dict.fromkeys(DIMENSIONS, "known")
    rows = [dict(identity="one", segment=segment, netPnl=-2, initialRiskUsd=1),
            dict(identity="two", segment=segment, netPnl=1, initialRiskUsd=None)]
    result = summarize(rows + rows, population="closed_positions")
    assert result["count"] == 2 and result["duplicatesExcluded"] == 2
    assert result["portfolioNetPnl"] == -1
    assert result["segments"][0]["expectancyR"] == -2
    assert result["segments"][0]["rSamples"] == 1
    assert summarize(rows, population="causal_labels")["portfolioNetPnl"] is None
