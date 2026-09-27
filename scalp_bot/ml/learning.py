"""Optional offline CPU learning. Imported only by explicit ML commands/worker."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import time

from .features import FEATURE_NAMES, FEATURE_SCHEMA
from .history.importer import sha256_file

CLASSES=("target_first","stop_first","timeout")


def load_rows(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def raw_matrix(rows):
    import numpy as np
    return np.asarray([r["features"]+[1 if r["side"]=="long" else -1] for r in rows],dtype=float)


def transform(raw, preprocessing):
    import numpy as np
    missing=np.isnan(raw)
    filled=np.where(missing,np.asarray(preprocessing["median"]),raw)
    standardized=(filled-np.asarray(preprocessing["mean"]))/np.asarray(preprocessing["scale"])
    return np.concatenate((standardized,missing.astype(float)),axis=1)


def temperature(probabilities, value):
    import numpy as np
    logits=np.log(np.clip(probabilities,1e-12,1))/value
    logits-=logits.max(axis=1,keepdims=True)
    probabilities=np.exp(logits)
    return probabilities/probabilities.sum(axis=1,keepdims=True)


def calibrate(probabilities, labels):
    from scipy.optimize import minimize_scalar
    from sklearn.metrics import log_loss
    result=minimize_scalar(lambda t:log_loss(labels,temperature(probabilities,t),labels=[0,1,2]),
                           bounds=(.25,4),method="bounded",options={"maxiter":40})
    return float(result.x)


def rule_probabilities(rows):
    import numpy as np
    imbalance=FEATURE_NAMES.index("trade_imbalance_5s")
    movement=FEATURE_NAMES.index("micro_move_5s_bps")
    result=[]
    for row in rows:
        sign=1 if row["side"]=="long" else -1
        a,b=row["features"][imbalance],row["features"][movement]
        aligned=a is not None and b is not None and sign*a>=.2 and sign*b>=2
        result.append([.55,.25,.20] if aligned else [.15,.25,.60])
    return np.asarray(result)


def metrics(rows, probabilities):
    import numpy as np
    from sklearn.metrics import log_loss
    labels=np.asarray([CLASSES.index(r["label"]) for r in rows])
    onehot=np.eye(3)[labels]
    selected=probabilities[:,0]>=.55
    confidence=probabilities.max(axis=1);correct=(probabilities.argmax(axis=1)==labels)
    ece=0
    for left in np.linspace(0,.9,10):
        mask=(confidence>=left)&(confidence<left+.1+1e-12)
        if mask.any():ece+=float(mask.mean()*abs(confidence[mask].mean()-correct[mask].mean()))
    payoff=np.asarray([r["net_usdt"] for r in rows])
    return dict(samples=len(rows),log_loss=float(log_loss(labels,probabilities,labels=[0,1,2])),
        brier=float(np.mean(np.sum((probabilities-onehot)**2,axis=1))),ece_10_bins=ece,
        episodes=len({r["episode"] for r in rows}) if all("episode" in r for r in rows) else None,
        selected_episodes=len({r["episode"] for r,keep in zip(rows,selected) if keep}) if all("episode" in r for r in rows) else None,
        selected=int(selected.sum()),coverage=float(selected.mean()),abstention=float(1-selected.mean()),
        selected_mean_net_usdt=float(payoff[selected].mean()) if selected.any() else None,
        payoff_scope="independent fixed nominal labels; overlapping capital not a portfolio")


def train(dataset_dir, output):
    import numpy as np
    import catboost,sklearn
    from catboost import CatBoostClassifier
    from sklearn.linear_model import LogisticRegression
    from threadpoolctl import threadpool_limits
    started=time.perf_counter()
    dataset_dir,output=Path(dataset_dir),Path(output)
    manifest=json.loads((dataset_dir/"manifest.json").read_text())
    if not manifest["training_ready"] or manifest["feature_schema"]!=FEATURE_SCHEMA:
        raise ValueError("dataset not ready or schema mismatch")
    if sha256_file(dataset_dir/"dataset.jsonl")!=manifest["dataset_sha256"]:
        raise ValueError("dataset hash mismatch")
    rows=load_rows(dataset_dir/"dataset.jsonl")
    split={key:[r for r in rows if r["split"]==key] for key in ("train","calibration","validation","test")}
    if any(len(v)<30 for v in split.values()):raise ValueError("insufficient temporal samples")
    labels=lambda rs:np.asarray([CLASSES.index(r["label"]) for r in rs])
    if set(labels(split["train"]))!={0,1,2}:raise ValueError("train must cover all classes")
    raw=raw_matrix(split["train"])
    medians=np.asarray([np.median(col[~np.isnan(col)]) if (~np.isnan(col)).any() else 0 for col in raw.T])
    filled=np.where(np.isnan(raw),medians,raw)
    mean,scale=filled.mean(axis=0),filled.std(axis=0)
    scale[scale<1e-12]=1
    preprocessing=dict(median=medians.tolist(),mean=mean.tolist(),scale=scale.tolist(),fit_split="train_only")
    matrix={k:transform(raw_matrix(v),preprocessing) for k,v in split.items()}
    parameters=manifest["plan_policy"]["catboost"]
    model=CatBoostClassifier(**parameters,loss_function="MultiClass",classes_count=3,
                            verbose=False,allow_writing_files=False)
    with threadpool_limits(limits=1):
        model.fit(matrix["train"],labels(split["train"]))
        logistic=LogisticRegression(C=1,max_iter=500,random_state=1729)
        logistic.fit(matrix["train"],labels(split["train"]))
    temperatures={"catboost":calibrate(model.predict_proba(matrix["calibration"]),labels(split["calibration"])),
                  "logistic":calibrate(logistic.predict_proba(matrix["calibration"]),labels(split["calibration"]))}
    output.mkdir(parents=True,exist_ok=False)
    model.save_model(str(output/"model.cbm"))
    np.savez(output/"logistic.npz",coef=logistic.coef_,intercept=logistic.intercept_)
    metadata=dict(schema_version=1,feature_schema=FEATURE_SCHEMA,feature_order=list(FEATURE_NAMES)+["plan_side"],
        transformed_order="standardized train-imputed features then missing flags",classes=CLASSES,
        preprocessing=preprocessing,temperatures=temperatures,parameters=parameters,
        dataset_sha256=manifest["dataset_sha256"],policy_sha256=manifest["policy_sha256"],
        policy_version=manifest["plan_policy"]["version"],plan_policy=manifest["plan_policy"],
        model_sha256=sha256_file(output/"model.cbm"),logistic_sha256=sha256_file(output/"logistic.npz"),
        python=platform.python_version(),platform=platform.platform(),versions=dict(catboost=catboost.__version__,
        sklearn=sklearn.__version__,numpy=np.__version__),order_authority=False,
        trained=True,admitted=False,untouched_external_test=manifest.get("untouched_external_test",False),
        decision_policy={"p_target_min":.55,"tuned_on_test":False,"frozen_before_test":True},scope=manifest.get("evidence_scope","technical first candidate, one audited session, no independent holdout"),
        retention="local until owner deletes; no automatic expiration",artifact_path=str(output.resolve()))
    metadata["model_version"]="catboost-"+manifest.get("candidate_version","impulse-v1")+":"+metadata["model_sha256"][:16]
    (output/"manifest.json").write_text(json.dumps(metadata,indent=2)+"\n")
    reloaded=Predictor(output)
    original=temperature(model.predict_proba(matrix["test"][:1]),temperatures["catboost"])
    repeated=reloaded.predict_rows(split["test"][:1])
    if not np.array_equal(original,repeated):raise AssertionError("saved model reload parity failed")
    report=evaluate(dataset_dir,output)
    report.update(training_seconds=time.perf_counter()-started,reload_exact=True)
    (output/"evaluation.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"model_version":metadata["model_version"],"training_seconds":report["training_seconds"],
        "test":report["splits"]["test"],"reload_exact":True}))
    return report


class Predictor:
    def __init__(self, directory):
        from catboost import CatBoostClassifier
        directory=Path(directory)
        self.metadata=json.loads((directory/"manifest.json").read_text())
        m=self.metadata
        if m["feature_schema"]!=FEATURE_SCHEMA or m["feature_order"]!=list(FEATURE_NAMES)+["plan_side"]:
            raise ValueError("model feature schema/order mismatch")
        if sha256_file(directory/"model.cbm")!=m["model_sha256"]:
            raise ValueError("model artifact checksum mismatch")
        self.model=CatBoostClassifier();self.model.load_model(str(directory/"model.cbm"))

    def predict_rows(self, rows):
        matrix=transform(raw_matrix(rows),self.metadata["preprocessing"])
        return temperature(self.model.predict_proba(matrix,thread_count=1),self.metadata["temperatures"]["catboost"])

    def predict(self,snapshot,side):
        if snapshot.names!=FEATURE_NAMES or snapshot.ref.feature_schema!=FEATURE_SCHEMA or side not in {"long","short"}:
            raise ValueError("snapshot schema or side mismatch")
        return self.predict_rows([dict(features=list(snapshot.values),side=side)])[0].tolist()


def evaluate(dataset_dir, model_dir):
    import numpy as np
    dataset_dir,model_dir=Path(dataset_dir),Path(model_dir)
    predictor=Predictor(model_dir)
    rows=load_rows(dataset_dir/"dataset.jsonl")
    metadata=predictor.metadata
    if sha256_file(dataset_dir/"dataset.jsonl")!=metadata["dataset_sha256"]:
        raise ValueError("evaluation dataset mismatch; register a new evaluation explicitly")
    if sha256_file(model_dir/"logistic.npz")!=metadata["logistic_sha256"]:
        raise ValueError("logistic artifact checksum mismatch")
    weights=np.load(model_dir/"logistic.npz",allow_pickle=False)
    report=dict(model_version=metadata["model_version"],scope=metadata["scope"],splits={},by_symbol={})
    train_rows=[r for r in rows if r["split"]=="train"]
    priors=np.asarray([sum(r["label"]==c for r in train_rows)/len(train_rows) for c in CLASSES])
    payouts=np.asarray([np.mean([r["net_usdt"] for r in train_rows if r["label"]==c]) for c in CLASSES])
    report["train_class_conditional_mean_net_usdt"]=dict(zip(CLASSES,payouts.tolist()))
    report["expectancy_warning"]="probability-weighted train class payouts are diagnostic estimates, not guaranteed conditional payouts or portfolio returns"
    for split in ("train","calibration","validation","test"):
        subset=[r for r in rows if r["split"]==split]
        matrix=transform(raw_matrix(subset),metadata["preprocessing"])
        logits=matrix@weights["coef"].T+weights["intercept"];logits-=logits.max(axis=1,keepdims=True)
        lp=np.exp(logits);lp/=lp.sum(axis=1,keepdims=True)
        lp=temperature(lp,metadata["temperatures"]["logistic"])
        cp=predictor.predict_rows(subset);rp=rule_probabilities(subset)
        report["splits"][split]={"catboost":metrics(subset,cp),"logistic":metrics(subset,lp),"fixed_rule":metrics(subset,rp),
            "train_prior":metrics(subset,np.tile(priors,(len(subset),1)))}
        report["splits"][split]["no_trade"]={"selected":0,"coverage":0.0,"net_usdt":0.0,"classification_metrics":None}
        report["splits"][split]["catboost"]["estimated_mean_net_from_train_payouts"]=float((cp@payouts).mean())
        if split=="test":
            for symbol in sorted({r["ref"]["symbol"] for r in subset}):
                indices=[i for i,r in enumerate(subset) if r["ref"]["symbol"]==symbol]
                report["by_symbol"][symbol]=metrics([subset[i] for i in indices],cp[indices])
    return report
