"""V3 research contracts: real first-prepared setups, never a side/time grid.

Pure context/features only. This module has no broker, credentials, risk engine,
worker startup or order API. Labels and training are separate offline steps.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

from .contracts import SnapshotRef
from .features import extract_context_features, ContextCoverage, FEATURE_SCHEMA


def intent_key(intent):
    identity = (intent.symbol, intent.strategy, intent.side, intent.scenario_id,
                intent.episode_key or intent.setup_id)
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


class PreparedDatasetCollector:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x", encoding="utf-8")
        self.seen = set()
        self.count = 0

    def observe_prepared(self, intent, context, *, capture_id, epoch, sequence,
                         available_ns, trade_seconds, deep_fresh, units_verified):
        source = SnapshotRef(capture_id, intent.symbol, epoch, sequence,
            context.observed_at_ms, available_ns, "capture:"+capture_id, FEATURE_SCHEMA)
        coverage = ContextCoverage(tuple(n for n in (5, 15, 60) if trade_seconds >= n), (),
            context.forming_candle is not None, deep_fresh, context.structure is not None, units_verified)
        return self.observe(intent, context, source, coverage)

    def observe(self, intent, context, source, coverage):
        key = intent_key(intent)
        if key in self.seen:
            return None
        if context.observed_at_ms != source.market_time_ms:
            raise ValueError("prepared snapshot must use contemporaneous context")
        snapshot = extract_context_features(context, source, coverage)
        row = dict(schema="prepared-setup-v3", identity=key, intent=asdict(intent),
            source=asdict(source), features=list(snapshot.values), coverage=asdict(coverage),
            economics="pending", outcome=None, population="causal_prepared_intents",
            trainingReady=False)
        self.stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False)+"\n")
        self.stream.flush()
        self.seen.add(key)
        self.count += 1
        return row

    def close(self):
        self.stream.close()


def purged_walk_forward(rows, windows, *, embargo_ms=60_000, held_out_symbols=()):
    """Global wall-time folds; purge whole future intervals and shared episodes.

    Each window is (train_start_ms, validation_start_ms, validation_end_ms).
    held_out_symbols never enter fitting; validation on those symbols is kept.
    Call again with each symbol held out for leave-symbol-out sensitivity.
    """
    if embargo_ms < 0:
        raise ValueError("negative embargo")
    folds = []
    for left, split, right in windows:
        if not left < split < right:
            raise ValueError("invalid chronological fold")
        train, validation, purged = [], [], []
        for i, row in enumerate(rows):
            start, end = row["available_wall_ms"], row["label_end_wall_ms"]
            if end < start:
                raise ValueError("outcome precedes features")
            if left <= start and end < split-embargo_ms and row["symbol"] not in held_out_symbols:
                train.append(i)
            elif split+embargo_ms <= start and end < right:
                validation.append(i)
            elif left <= start < right:
                purged.append(i)
        validation_episodes = {rows[i]["episode"] for i in validation}
        leaking = [i for i in train if rows[i]["episode"] in validation_episodes]
        train = [i for i in train if i not in leaking]
        folds.append(dict(train=train, validation=validation, purged=sorted(purged+leaking),
            heldOutValidation=[i for i in validation if rows[i]["symbol"] in held_out_symbols],
            trainStart=left, validationStart=split, validationEnd=right, embargoMs=embargo_ms))
    return folds


@dataclass(frozen=True, slots=True)
class PreparedForecast:
    """Advisory rank/veto output; no side, size or executable plan authority."""
    identity: str
    source: SnapshotRef
    model_hash: str
    produced_mono_ns: int
    expires_mono_ns: int
    p_target_before_stop: float
    expected_net_r: float
    expected_mfe_bps: float
    expected_mae_bps: float
    expected_event_ms: float

    def __post_init__(self):
        values = (self.p_target_before_stop, self.expected_net_r, self.expected_mfe_bps,
                  self.expected_mae_bps, self.expected_event_ms)
        if not all(math.isfinite(v) for v in values) or not 0 <= self.p_target_before_stop <= 1:
            raise ValueError("invalid economic forecast")
        if not self.source.available_mono_ns <= self.produced_mono_ns < self.expires_mono_ns:
            raise ValueError("invalid forecast time")


def forecast_matches(forecast, intent, source, now_ns, model_hash):
    return (forecast.identity == intent_key(intent) and forecast.source == source
            and forecast.model_hash == model_hash
            and forecast.produced_mono_ns <= now_ns < forecast.expires_mono_ns)
