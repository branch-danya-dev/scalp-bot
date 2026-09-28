"""Identity-bound advisory V3 rank/veto at the existing admission boundary."""
from .prepared_dataset import intent_key, forecast_matches


class PreparedRanker:
    def __init__(self, model_hash, emit, *, minimum_net_r=0., mode="shadow"):
        if not isinstance(model_hash, str) or len(model_hash) != 64 or any(c not in "0123456789abcdef" for c in model_hash):
            raise ValueError("exact model SHA256 required")
        if mode not in {"shadow", "enforce"}:
            raise ValueError("unknown V3 mode")
        self.model_hash, self.emit = model_hash, emit
        self.minimum_net_r, self.mode = minimum_net_r, mode
        self.sources, self.forecasts = {}, {}

    def register(self, intent, source):
        self.sources.setdefault(intent_key(intent), source)

    def register_prepared(self, intent, source):
        from .contracts import SnapshotRef
        self.register(intent, SnapshotRef(**source))

    def receive(self, forecast):
        if forecast.model_hash != self.model_hash:
            return False
        source = self.sources.get(forecast.identity)
        if source is None or source != forecast.source:
            return False
        self.forecasts[forecast.identity] = forecast
        return True

    def assess(self, intent, now_ns):
        key = intent_key(intent)
        forecast, source = self.forecasts.get(key), self.sources.get(key)
        valid = forecast is not None and source is not None and forecast_matches(
            forecast, intent, source, now_ns, self.model_hash)
        reason = "ranked" if valid and forecast.expected_net_r >= self.minimum_net_r else "negative_expected_net_r" if valid else "missing_or_invalid_forecast"
        allowed = reason == "ranked"
        self.emit("prepared_forecast_decision", dict(identity=key, modelHash=self.model_hash, mode=self.mode,
            nowNs=now_ns, reason=reason, wouldVeto=not allowed,
            expectedNetR=forecast.expected_net_r if valid else None))
        return self.mode == "shadow" or allowed, forecast.expected_net_r if valid else float("-inf")

    def rank(self, opportunities, now_ns):
        from ..admission import PreparedIntent
        assessed = []
        for index, opportunity in enumerate(opportunities):
            intent = PreparedIntent(**opportunity.decision.details["admission"]["intent"])
            allowed, score = self.assess(intent, now_ns)
            if allowed:
                assessed.append((score, index, opportunity))
        if self.mode == "enforce":
            assessed.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in assessed]
