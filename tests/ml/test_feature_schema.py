import json
from pathlib import Path
from scalp_bot.ml.features import schema_description


def test_checked_in_schema_matches_adapter():
    path = Path(__file__).resolve().parents[2] / "research/ml/market-context-v1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == schema_description()
