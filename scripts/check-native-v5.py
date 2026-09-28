"""Offline v5 architecture witness. Does not authorize or launch market data."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    for key in list(os.environ):
        if key.upper().startswith("SCALP_"): del os.environ[key]
    os.environ["SCALP_DISABLE_DOTENV"] = "1"
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--model-dir")
    args = parser.parse_args()
    from scalp_bot.ml.native_fixture import run_fixture
    report = asyncio.run(run_fixture(args.output, model_dir=args.model_dir))
    print(json.dumps({key:report[key] for key in ("fixture", "counts", "productionAtoF", "controlledW20")}))
