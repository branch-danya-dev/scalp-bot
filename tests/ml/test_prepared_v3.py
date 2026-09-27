from dataclasses import replace
import json

import pytest

from scalp_bot.admission import PreparedIntent
from scalp_bot.ml.contracts import SnapshotRef
from scalp_bot.ml.features import FEATURE_SCHEMA, ContextCoverage
from scalp_bot.ml.prepared_dataset import PreparedDatasetCollector, purged_walk_forward, PreparedForecast, forecast_matches, intent_key
from scalp_bot.domain import Action
from test_features import context


def test_prepared_snapshot_is_once_per_causal_setup_and_does_not_resample_after_rejection(tmp_path):
    ctx = replace(context(), symbol="AAA")
    intent = PreparedIntent("AAA", "level_breakout", "long", "setup", "scenario", "episode", 1, 100, 99, 102)
    ref = SnapshotRef("capture", "AAA", 0, 1, ctx.observed_at_ms, 100, "domain", FEATURE_SCHEMA)
    collector = PreparedDatasetCollector(tmp_path/"prepared.jsonl")
    try:
        row = collector.observe(intent, ctx, ref, ContextCoverage())
        assert row["intent"]["side"] == "long" and row["economics"] == "pending"
        assert collector.observe(intent, replace(ctx, last_price=120), replace(ref, available_mono_ns=200), ContextCoverage()) is None
        # Setup labels may evolve while the causal owner/episode stays fixed.
        assert collector.observe(replace(intent, setup_id="renamed", entry=101), ctx,
            replace(ref, available_mono_ns=300), ContextCoverage()) is None
        assert collector.count == 1
        assert len((tmp_path/"prepared.jsonl").read_text().splitlines()) == 1
    finally:
        collector.close()


def test_walk_forward_purges_future_windows_episodes_and_held_out_symbols():
    rows = [dict(available_wall_ms=s, label_end_wall_ms=e, symbol=symbol, episode=episode)
            for s,e,symbol,episode in [(0,10,"AAA","1"),(80,110,"AAA","2"),
                (10,20,"ENA","3"),(20,30,"AAA","shared"),(120,140,"AAA","shared"),
                (125,145,"ENA","4"),(190,210,"AAA","5")]]
    fold = purged_walk_forward(rows, [(0,100,200)], embargo_ms=10, held_out_symbols=("ENA",))[0]
    assert fold["train"] == [0]
    assert fold["validation"] == [4,5]
    assert fold["heldOutValidation"] == [5]
    assert set(fold["purged"]) == {1,2,3,6}


def test_ranker_output_cannot_match_another_intent_or_future_source():
    intent = PreparedIntent("AAA", "level_breakout", "long", "setup", "scenario", "episode", 1, 100, 99, 102)
    ref = SnapshotRef("capture", "AAA", 0, 1, 1, 100, "domain", FEATURE_SCHEMA)
    forecast = PreparedForecast(intent_key(intent), ref, "hash", 150, 200, .6, .1, 20, 10, 1000)
    assert forecast_matches(forecast, intent, ref, 160, "hash")
    assert not forecast_matches(forecast, replace(intent, side="short"), ref, 160, "hash")
    assert not forecast_matches(forecast, intent, ref, 201, "hash")
    assert not forecast_matches(forecast, intent, replace(ref, source_sequence=2), 160, "hash")
    with pytest.raises(ValueError):
        replace(forecast, expected_net_r=float("nan"))
