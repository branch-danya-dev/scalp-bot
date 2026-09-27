"""Explicit offline W2 commands; no market or execution startup."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("replay", "study", "evaluate", "train", "test"))
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--kind", choices=("logistic", "catboost"))
    parser.add_argument("--evidence", type=Path, help="Verified dataset manifest with primary-chain and label replay receipts")
    args = parser.parse_args()
    if args.command == "replay":
        from scalp_bot.ml.wave2_replay import replay
        result = replay(args.source, args.output)
    else:
        rows = [json.loads(line) for line in args.source.read_text(encoding="utf-8").splitlines()]
        protocol = json.loads(args.protocol.read_text()) if args.protocol else None
        evidence = json.loads(args.evidence.read_text()) if args.evidence else None
        if args.command == "test":
            from scalp_bot.ml.prepared_learning import evaluate_test
            result = evaluate_test(rows, args.output, evidence=evidence)
        else:
            if protocol is None:
                parser.error("a preregistered protocol is required")
            if args.command == "train":
                if args.kind is None:
                    parser.error("explicit previously selected baseline is required")
                from scalp_bot.ml.prepared_learning import train_frozen
                result = train_frozen(rows, protocol, args.output, kind=args.kind, evidence=evidence)
            elif args.command == "evaluate":
                from scalp_bot.ml.prepared_learning import evaluate
                result = evaluate(rows, protocol, evidence=evidence)
            else:
                from scalp_bot.ml.cross_venue_study import study
                result = study(rows, protocol["windows"], embargo_ms=protocol["embargo_ms"])
            if args.command != "train":
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k in {"status", "trainingReady", "promotionAuthorized", "prepared", "labels", "datasetHash"}}))


if __name__ == "__main__":
    main()
