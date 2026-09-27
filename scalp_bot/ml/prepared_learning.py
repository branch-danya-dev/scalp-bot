"""Offline V3 economic ranking. Optional ML imports never enter the market loop.

Fixed logistic/Ridge and CatBoost baselines, train-only preprocessing, separate
calibration, purged global wall-time folds and LOSO. Test is a separate command
after selection; all outputs retain exact dataset/protocol/model provenance.
"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from .prepared_dataset import purged_walk_forward
from .learning import transform


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verify_dataset_evidence(rows, evidence):
    if not evidence or evidence.get("datasetHash") != digest(rows) or evidence.get("trainingReady") is not True:
        raise ValueError("verified dataset evidence required before fitting/testing")
    for capture in {r["capture_id"] for r in rows}:
        proof = evidence.get("captures", {}).get(capture, {})
        if proof.get("primaryIntegrity") != "MET" or proof.get("labelReplay") != "MET":
            raise ValueError("dataset evidence missing primary integrity or executable replay")
        for key in ("sourceHash", "configHash", "runtimeHash"):
            value = proof.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("dataset evidence missing exact capture hashes")


def eligible(rows):
    result, seen = [], set()
    for row in rows:
        identity = (row["capture_id"], row["identity"])
        if identity in seen:
            raise ValueError("duplicate prepared identity")
        seen.add(identity)
        if row.get("censor_reason") or not row.get("trainingReady"):
            continue
        required = ("available_wall_ms", "label_end_wall_ms", "realized_net_r")
        if any(not isinstance(row.get(k), (float, int)) or not math.isfinite(row[k]) for k in required):
            raise ValueError("missing executable/global wall provenance")
        if row["label_end_wall_ms"] < row["available_wall_ms"] or type(row.get("target_before_stop")) is not bool:
            raise ValueError("invalid outcome interval/class")
        if row["source"]["clock_domain"] != "capture:"+row["capture_id"]:
            raise ValueError("capture clock domain mismatch")
        result.append(row)
    return result


def matrix(rows):
    import numpy as np
    def values(row):
        cross = row.get("crossVenue", {})
        extra = []
        for venue in ("binance", "okx"):
            v = cross.get("venues", {}).get(venue, {})
            extra += [v.get("returnsBps", {}).get(str(window)) for window in (500, 1000)]
            extra += [cross.get("divergenceBps", {}).get(venue), v.get("ofiUsd"), v.get("tradeImpulse")]
        extra += [cross.get(k) for k in ("consensusStrength", "consensusDirection", "coverage")]
        return row["features"]+[1 if row["side"] == "long" else -1]+extra
    return np.asarray([values(row) for row in rows], dtype=float)


def splits(rows, window, embargo_ms, held_out=()):
    left, calibration, validation, right = window
    if not left < calibration < validation < right:
        raise ValueError("train/calibration/validation must be ordered")
    fold = purged_walk_forward(rows, [(left, validation, right)], embargo_ms=embargo_ms,
        held_out_symbols=held_out)[0]
    fitting = purged_walk_forward(rows, [(left, calibration, validation-embargo_ms)],
        embargo_ms=embargo_ms, held_out_symbols=held_out)[0]
    train = fitting["train"]
    calibrate = [i for i in fitting["validation"] if rows[i]["symbol"] not in held_out]
    valid = fold["validation"]
    future_episodes = {rows[i]["episode"] for i in valid}
    calibrate = [i for i in calibrate if rows[i]["episode"] not in future_episodes]
    future_episodes |= {rows[i]["episode"] for i in calibrate}
    train = [i for i in train if rows[i]["episode"] not in future_episodes]
    return dict(train=train, calibration=calibrate, validation=valid,
        heldOutValidation=[i for i in valid if rows[i]["symbol"] in held_out])


def fit(rows, split, kind):
    import numpy as np
    from sklearn.linear_model import LogisticRegression, Ridge
    from threadpoolctl import threadpool_limits
    training = [rows[i] for i in split["train"]]
    calibration = [rows[i] for i in split["calibration"]]
    if len(training) < 30 or len(calibration) < 30 or any(len({r["target_before_stop"] for r in rs}) < 2 for rs in (training, calibration)):
        raise ValueError("insufficient fitting/calibration classes or samples")
    raw = matrix(training)
    medians = np.array([np.median(c[~np.isnan(c)]) if (~np.isnan(c)).any() else 0 for c in raw.T])
    filled = np.where(np.isnan(raw), medians, raw)
    scale = filled.std(axis=0)
    scale[scale < 1e-12] = 1
    prep = dict(median=medians.tolist(), mean=filled.mean(axis=0).tolist(), scale=scale.tolist())
    x = transform(raw, prep)
    y = np.array([int(r["target_before_stop"]) for r in training])
    net = np.array([r["realized_net_r"] for r in training])
    if kind == "logistic":
        classifier = LogisticRegression(C=1, max_iter=500, random_state=1729)
        ranker = Ridge(alpha=1)
    elif kind == "catboost":
        from catboost import CatBoostClassifier, CatBoostRegressor
        params = dict(iterations=200, depth=4, learning_rate=.05, random_seed=1729,
            thread_count=1, verbose=False, allow_writing_files=False)
        classifier, ranker = CatBoostClassifier(**params), CatBoostRegressor(**params)
    else:
        raise ValueError("unknown fixed baseline")
    with threadpool_limits(limits=1):
        classifier.fit(x, y)
        ranker.fit(x, net)
        cx = transform(matrix(calibration), prep)
        p = np.clip(classifier.predict_proba(cx)[:, 1], 1e-9, 1-1e-9)
        calibrator = LogisticRegression(C=1, random_state=1729)
        calibrator.fit(np.log(p/(1-p)).reshape(-1, 1), [int(r["target_before_stop"]) for r in calibration])
    return dict(kind=kind, classifier=classifier, ranker=ranker, calibrator=calibrator, preprocessing=prep)


def predict(model, rows):
    import numpy as np
    x = transform(matrix(rows), model["preprocessing"])
    p = np.clip(model["classifier"].predict_proba(x)[:, 1], 1e-9, 1-1e-9)
    return model["calibrator"].predict_proba(np.log(p/(1-p)).reshape(-1, 1))[:, 1], model["ranker"].predict(x)


def report(rows, probabilities, scores):
    import numpy as np
    if not rows:
        return dict(samples=0, status="INCONCLUSIVE")
    net = np.array([r["realized_net_r"] for r in rows])
    y = np.array([int(r["target_before_stop"]) for r in rows])
    order = np.argsort(-np.asarray(scores), kind="stable")
    curves = {}
    for fraction in (1., .5, .25, .1):
        selected = order[:max(1, math.ceil(len(rows)*fraction))]
        curves[str(fraction)] = dict(samples=len(selected), meanNetR=float(net[selected].mean()),
            coverage=len(selected)/len(rows), abstention=1-len(selected)/len(rows))
    calibration = []
    for index in range(10):
        left = index/10
        ix = (probabilities >= left) & ((probabilities < (index+1)/10) if index < 9 else (probabilities <= 1))
        calibration.append(dict(lower=float(left), samples=int(ix.sum()),
            probability=float(np.mean(probabilities[ix])) if ix.any() else None,
            frequency=float(np.mean(y[ix])) if ix.any() else None))
    concentration = {}
    for dimension in ("symbol", "capture_id"):
        counts = Counter(r[dimension] for r in rows)
        concentration[dimension] = dict(counts=counts, largestShare=max(counts.values())/len(rows))
    concentration["regime"] = dict(Counter(r["segment"].get("localRegime", "unknown") for r in rows))
    return dict(samples=len(rows), brier=float(np.mean((probabilities-y)**2)), calibration=calibration,
        economicRanking=curves, topQuarterImprovementR=curves["0.25"]["meanNetR"]-curves["1.0"]["meanNetR"],
        concentration=concentration, portfolioPnl=None, scope="identical_prepared_membership_independent_labels")


def evaluate(rows, protocol, evidence=None):
    """Development folds only. Test capture payloads are explicitly prohibited."""
    raw_rows = rows
    rows = eligible(rows)
    test_ids = set(protocol["untouched_test_captures"])
    if not test_ids or any(r["capture_id"] in test_ids for r in rows):
        raise ValueError("untouched test must be reserved outside development data")
    verify_dataset_evidence(raw_rows, evidence)
    if len({r["capture_id"] for r in rows}) < 3:
        raise ValueError("at least three development captures required")
    embargo = protocol["embargo_ms"]
    if embargo < 60_000:
        raise ValueError("V3 embargo cannot be weakened below preregistered 60s")
    results = []
    symbols = sorted({r["symbol"] for r in rows})
    if len(symbols) < 2:
        raise ValueError("single-symbol evidence cannot validate V3")
    for fold_id, window in enumerate(protocol["windows"]):
        for held in [None]+symbols:
            split = splits(rows, window, embargo, (held,) if held else ())
            indices = split["heldOutValidation"] if held else split["validation"]
            valid = [rows[i] for i in indices]
            for kind in ("logistic", "catboost"):
                item = dict(fold=fold_id, heldOutSymbol=held, kind=kind, splitHash=digest(split))
                try:
                    if len(valid) < 30:
                        raise ValueError("insufficient validation membership")
                    model = fit(rows, split, kind)
                    item.update(status="evaluated", metrics=report(valid, *predict(model, valid)))
                except ValueError as exc:
                    item.update(status="INCONCLUSIVE", reason=str(exc))
                results.append(item)
    return dict(schema="prepared-v3-evaluation-v1", datasetHash=digest(rows), protocolHash=digest(protocol),
        folds=results, promotionAuthorized=False, testEvaluated=False,
        requiredGates=["independent_economic_stability", "portfolio_replay", "untouched_test", "runtime_latency"])


def train_frozen(rows, protocol, output, *, kind, evidence=None):
    """Explicit selected baseline; save artifacts before any untouched-test access."""
    verify_dataset_evidence(rows, evidence)
    import numpy as np
    rows = eligible(rows)
    if protocol["embargo_ms"] < 60_000 or len({r["capture_id"] for r in rows}) < 3 or len({r["symbol"] for r in rows}) < 2:
        raise ValueError("insufficient independent development evidence or embargo")
    if any(r["capture_id"] in set(protocol["untouched_test_captures"]) for r in rows):
        raise ValueError("test capture present in fitting data")
    split = splits(rows, protocol["final_window"], protocol["embargo_ms"])
    model = fit(rows, split, kind)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if kind == "catboost":
        model["classifier"].save_model(str(output/"classifier.cbm"))
        model["ranker"].save_model(str(output/"ranker.cbm"))
    else:
        np.savez(output/"linear.npz", classifier_coef=model["classifier"].coef_,
            classifier_intercept=model["classifier"].intercept_, ranker_coef=model["ranker"].coef_,
            ranker_intercept=model["ranker"].intercept_)
    metadata = dict(kind=kind, datasetHash=digest(rows), datasetEvidenceHash=digest(evidence), protocolHash=digest(protocol), splitHash=digest(split),
        preprocessing=model["preprocessing"], calibration=dict(coef=model["calibrator"].coef_.tolist(),
            intercept=model["calibrator"].intercept_.tolist()),
        files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()},
        untouchedTestCaptures=protocol["untouched_test_captures"],
        trainingEndWallMs=protocol["final_window"][2], featureSchema="prepared-v3-cross-venue-v1",
        promotionAuthorized=False)
    metadata["modelHash"] = digest(metadata)
    (output/"manifest.json").write_text(json.dumps(metadata, indent=2)+"\n", encoding="utf-8")
    return metadata


def evaluate_test(rows, model_dir, evidence=None):
    """One immutable test receipt, with safe numeric/CatBoost model deserialization."""
    verify_dataset_evidence(rows, evidence)
    import numpy as np
    directory = Path(model_dir)
    metadata = json.loads((directory/"manifest.json").read_text())
    expected = metadata.pop("modelHash")
    if digest(metadata) != expected:
        raise ValueError("model manifest mismatch")
    for name, checksum in metadata["files"].items():
        if Path(name).name != name or hashlib.sha256((directory/name).read_bytes()).hexdigest() != checksum:
            raise ValueError("model artifact mismatch")
    rows = eligible(rows)
    if not rows or any(r["capture_id"] not in metadata["untouchedTestCaptures"] or
            r["available_wall_ms"] <= metadata["trainingEndWallMs"]+60_000 for r in rows):
        raise ValueError("test capture identity/time mismatch")
    # Reserve first, so failed attempts cannot be silently overwritten or retried.
    with (directory/"test-receipt.json").open("x", encoding="utf-8") as receipt:
        receipt.write(json.dumps(dict(status="STARTED", modelHash=expected, datasetHash=digest(rows))))
    x = transform(matrix(rows), metadata["preprocessing"])
    if metadata["kind"] == "catboost":
        from catboost import CatBoostClassifier, CatBoostRegressor
        classifier, ranker = CatBoostClassifier(), CatBoostRegressor()
        classifier.load_model(str(directory/"classifier.cbm"))
        ranker.load_model(str(directory/"ranker.cbm"))
        probabilities, scores = classifier.predict_proba(x)[:, 1], ranker.predict(x)
    else:
        with np.load(directory/"linear.npz", allow_pickle=False) as data:
            logits = (x@data["classifier_coef"].T+data["classifier_intercept"]).ravel()
            probabilities = 1/(1+np.exp(-np.clip(logits, -700, 700)))
            scores = x@data["ranker_coef"]+data["ranker_intercept"]
    p = np.clip(probabilities, 1e-9, 1-1e-9)
    calibrated_logits = np.log(p/(1-p))*metadata["calibration"]["coef"][0][0]+metadata["calibration"]["intercept"][0]
    calibrated = 1/(1+np.exp(-np.clip(calibrated_logits, -700, 700)))
    result = dict(modelHash=expected, datasetHash=digest(rows), metrics=report(rows, calibrated, scores),
        promotionAuthorized=False, status="evaluated_once")
    (directory/"test-receipt.json").write_text(json.dumps(result, indent=2)+"\n")
    return result
