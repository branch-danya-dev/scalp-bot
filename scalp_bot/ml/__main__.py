"""Read-only M0 status; does not load a model, exchange client or environment."""
import argparse
import json

from . import MODEL_TRAINED, RUNTIME_CONNECTED, STAGE


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps({
        "stage": STAGE, "model_trained": MODEL_TRAINED,
        "runtime_connected": RUNTIME_CONNECTED, "order_authority": False,
        "next_step": "M1: sealed capture inventory and causal feature dataset",
        "roadmap": "docs/ml/ROADMAP.md",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
