"""Explicit offline ML commands. Default status imports no optional dependencies."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command")
    build=sub.add_parser("build-dataset")
    build.add_argument("--source",type=Path,required=True);build.add_argument("--output",type=Path,required=True)
    build.add_argument("--capture-id",required=True)
    train_parser=sub.add_parser("train");train_parser.add_argument("--dataset",type=Path,required=True);train_parser.add_argument("--output",type=Path,required=True)
    evaluate_parser=sub.add_parser("evaluate");evaluate_parser.add_argument("--dataset",type=Path,required=True);evaluate_parser.add_argument("--model",type=Path,required=True)
    predict_parser=sub.add_parser("predict");predict_parser.add_argument("--dataset",type=Path,required=True);predict_parser.add_argument("--model",type=Path,required=True);predict_parser.add_argument("--row",type=int,default=0)
    shadow=sub.add_parser("shadow");shadow.add_argument("--dataset",type=Path,required=True);shadow.add_argument("--model",type=Path,required=True);shadow.add_argument("--output",type=Path,required=True);shadow.add_argument("--limit",type=int,default=200)
    args=parser.parse_args(argv)
    if args.command=="build-dataset":
        from .dataset import build_dataset
        build_dataset(args.source,args.output,args.capture_id)
    elif args.command=="train":
        from .learning import train
        train(args.dataset,args.output)
    elif args.command=="evaluate":
        from .learning import evaluate
        print(json.dumps(evaluate(args.dataset,args.model),indent=2))
    elif args.command=="predict":
        from .learning import Predictor,load_rows,CLASSES
        rows=load_rows(args.dataset/"dataset.jsonl");row=rows[args.row]
        p=Predictor(args.model)
        print(json.dumps(dict(model_version=p.metadata["model_version"],source=row["ref"],side=row["side"],
            probabilities=dict(zip(CLASSES,p.predict_rows([row])[0].tolist())),order_authority=False),indent=2))
    elif args.command=="shadow":
        from .shadow import run_shadow
        run_shadow(args.dataset,args.model,args.output,args.limit)
    else:
        from . import STAGE,MODEL_TRAINED,RUNTIME_CONNECTED
        print(json.dumps(dict(stage=STAGE,model_trained=MODEL_TRAINED,runtime_connected=RUNTIME_CONNECTED,
             order_authority=False,next_step="explicit build-dataset/train/evaluate/predict/shadow commands")))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
