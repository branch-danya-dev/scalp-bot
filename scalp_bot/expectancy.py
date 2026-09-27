from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import math
from .setup_segments import segment_key, finite


@dataclass(slots=True)
class StrategyExpectancy:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_usd: float = 0.0
    sum_r: float = 0.0
    win_pnl_usd: float = 0.0
    loss_pnl_usd: float = 0.0

    def record(
        self,
        net_pnl_usd: float,
        initial_risk_usd: float,
    ) -> None:
        self.trades += 1
        self.net_pnl_usd += net_pnl_usd
        if initial_risk_usd > 0:
            self.sum_r += net_pnl_usd / initial_risk_usd
        if net_pnl_usd > 0:
            self.wins += 1
            self.win_pnl_usd += net_pnl_usd
        elif net_pnl_usd < 0:
            self.losses += 1
            self.loss_pnl_usd += net_pnl_usd

    def public(
        self,
        *,
        min_samples: int,
        minimum_expectancy_r: float,
    ) -> dict:
        win_rate = (
            self.wins / self.trades
            if self.trades > 0
            else 0.0
        )
        avg_win = (
            self.win_pnl_usd / self.wins
            if self.wins > 0
            else 0.0
        )
        avg_loss = (
            self.loss_pnl_usd / self.losses
            if self.losses > 0
            else 0.0
        )
        expectancy_r = (
            self.sum_r / self.trades
            if self.trades > 0
            else 0.0
        )
        ready = self.trades >= max(1, min_samples)
        if not ready:
            status = "insufficient_samples"
        elif expectancy_r >= minimum_expectancy_r:
            status = "positive"
        else:
            status = "negative"
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "winRate": win_rate,
            "avgWinUsd": avg_win,
            "avgLossUsd": avg_loss,
            "netPnlUsd": self.net_pnl_usd,
            "expectancyR": expectancy_r,
            "minimumExpectancyR": minimum_expectancy_r,
            "minSamples": min_samples,
            "sampleReady": ready,
            "status": status,
        }


class StrategyExpectancyBook:
    def __init__(self, strategies: list[str]) -> None:
        self._stats = {
            strategy: StrategyExpectancy()
            for strategy in strategies
        }

    def record(
        self,
        strategy: str,
        *,
        net_pnl_usd: float,
        initial_risk_usd: float,
    ) -> None:
        self._stats.setdefault(
            strategy,
            StrategyExpectancy(),
        ).record(
            net_pnl_usd,
            initial_risk_usd,
        )

    def snapshot(
        self,
        strategy: str,
        *,
        min_samples: int,
        minimum_expectancy_r: float,
    ) -> dict:
        return self._stats.setdefault(
            strategy,
            StrategyExpectancy(),
        ).public(
            min_samples=min_samples,
            minimum_expectancy_r=minimum_expectancy_r,
        )


class SegmentExpectancyBook:
    """Only completed unique positions contribute, never overlapping path labels."""
    def __init__(self):
        self.stats = {}
        self.seen = set()
        self.evidence = {}

    def record(self, observation):
        risk, net = observation.get("initialRiskUsd"), observation.get("netPnl")
        if not finite(risk) or risk <= 0 or not finite(net) or observation["identity"] in self.seen:
            return
        self.seen.add(observation["identity"])
        key = segment_key(observation["segment"])
        self.stats.setdefault(key, StrategyExpectancy()).record(net, risk)
        self._record_evidence(observation, "closed_positions")

    def _record_evidence(self, observation, population):
        evidence = self.evidence.setdefault((population, segment_key(observation["segment"])), SegmentEvidence())
        evidence.record(observation)

    def record_shadow(self, observation):
        """Same store/key, distinct population. Censored/overlapping labels do not fit."""
        if observation.get("censor_reason") or not observation.get("executable", False):
            return False
        risk, net = observation.get("initialRiskUsd"), observation.get("netPnl")
        if not finite(risk) or risk <= 0 or not finite(net):
            return False
        evidence = self.evidence.setdefault(("shadow", segment_key(observation["segment"])), SegmentEvidence())
        return evidence.record(observation, require_interval=True)

    def lifecycle_evidence(self, segment, *, population="shadow"):
        return self.evidence.get((population, segment_key(segment)), SegmentEvidence()).public()

    def assess(self, segment, *, mode, min_samples, minimum_expectancy_r):
        if mode not in {"off", "shadow", "enforce"}:
            raise ValueError("unknown segment gate mode")
        key = segment_key(segment)
        stats = self.stats.get(key, StrategyExpectancy()).public(
            min_samples=min_samples, minimum_expectancy_r=minimum_expectancy_r)
        ready = stats["sampleReady"] and "unknown" not in key
        would_veto = ready and stats["status"] == "negative"
        return dict(**stats, segment=segment, mode=mode, evidenceReady=ready,
            wouldVeto=would_veto, blocked=mode == "enforce" and would_veto,
            source="prior_unique_closed_positions", payoutMeaning="empirical_net_expectancy")


class SegmentEvidence:
    """Bounded recent sample + Welford long-term moments; no portfolio PnL claim."""
    def __init__(self):
        self.n = 0
        self.mean = self.m2 = self.cumulative_r = self.peak_r = 0.0
        self.recent = deque(maxlen=200)
        self.seen = set()
        self.captures = set()
        self.last_end = {}

    def record(self, row, *, require_interval=False):
        capture = row.get("capture_id")
        identity = (capture, row["identity"])
        if identity in self.seen:
            return False
        if require_interval:
            start, end = row.get("available_wall_ms"), row.get("label_end_wall_ms")
            symbol = row.get("symbol")
            if not capture or not symbol or not finite(start) or not finite(end) or end < start:
                return False
            # One causal nonoverlapping outcome per symbol/segment, globally in wall time.
            if start <= self.last_end.get(symbol, -1):
                return False
            self.last_end[symbol] = end
        value = row["netPnl"] / row["initialRiskUsd"]
        if not finite(value):
            return False
        self.seen.add(identity)
        if capture:
            self.captures.add(capture)
        self.n += 1
        delta = value-self.mean
        self.mean += delta/self.n
        self.m2 += delta*(value-self.mean)
        self.recent.append(value)
        self.cumulative_r += value
        self.peak_r = max(self.peak_r, self.cumulative_r)
        return True

    def public(self):
        def bounds(n, mean, m2):
            if n < 2:
                return dict(samples=n, meanR=None, lowerR=None, upperR=None)
            # Descriptive normal interval; independent-period promotion remains a separate gate.
            half = 1.96*math.sqrt(max(0.0, m2)/(n-1)/n)
            return dict(samples=n, meanR=mean, lowerR=mean-half, upperR=mean+half)
        recent_mean = sum(self.recent)/len(self.recent) if self.recent else 0
        return dict(longTerm=bounds(self.n, self.mean, self.m2),
            recent=bounds(len(self.recent), recent_mean, sum((v-recent_mean)**2 for v in self.recent)),
            drawdownR=self.peak_r-self.cumulative_r, captures=len(self.captures),
            uncertainty="descriptive_normal_95_nonoverlapping_samples",
            scope="segment_diagnostic_not_portfolio_pnl")
